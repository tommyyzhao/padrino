"""Private friend lobby create/configure routes (US-147).

A host creates a private lobby and configures the human-multiplayer game it will
launch: the ruleset/size (``mini7_v1`` / ``bench10_v1``), the per-game
``identity_mode`` (default ANONYMOUS), a static ``theme_pack_id``, the bot
pre-pick (a list of human-eligible model ``agent_build`` ids) vs curated
auto-fill, and stakes pinned ``CASUAL``. ``GET /lobbies/{id}`` returns a
member-scoped view whose composition is COUNTS ONLY — never a per-seat human/AI
map — produced by the single canonical
:func:`padrino.core.composition.composition_summary` (US-126/US-142).

There is no public matchmaking in v1 (decision 2): a lobby is reachable only by
its members (and, in US-148, by an invite link).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.deps import get_session, get_session_factory
from padrino.api.human_auth import HumanPrincipalContext, require_human
from padrino.api.lobby_launch import (
    AutoFillPoolError,
    LobbyNotLaunchableError,
    launch_lobby,
)
from padrino.api.lobby_presence import (
    MemberView,
    roster_view,
    should_auto_cancel,
    stale_member_ids,
)
from padrino.api.lobby_state import stream_lobby_state
from padrino.core.composition import CompositionSummary, composition_summary
from padrino.core.enums import IdentityMode, LobbySeatKind, LobbyStakes, LobbyStatus
from padrino.core.rulesets import get_ruleset
from padrino.db.models import Lobby
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import lobbies as lobbies_repo
from padrino.economics.human_cost_governance import (
    ACTION_CREATE,
    ACTION_JOIN,
    ACTION_LAUNCH,
    HumanAdmitDecision,
    admit_human,
    bind_admission_slots,
    release_admission_for_lobby,
    release_admission_for_member,
    release_inference_reservations_for_lobby,
    rollback_admission_decision,
)

router = APIRouter()


def _stale_seconds() -> float:
    from padrino.settings import get_settings

    return get_settings().padrino_lobby_presence_stale_seconds


def _idle_seconds() -> float:
    from padrino.settings import get_settings

    return get_settings().padrino_lobby_idle_cancel_seconds


# Map an admission-denial reason to its HTTP status. A breaker-open or per-user
# cap denial is a 429 (too many requests / rate-limited admission); the body
# carries the structured reason so the caller never parses prose.
_ADMISSION_DENIED_STATUS = status.HTTP_429_TOO_MANY_REQUESTS


async def _enforce_admission(
    request: Request,
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    action: str,
) -> HumanAdmitDecision:
    """Gate a human create/join/launch admission against per-user caps + breaker.

    The principal is the OAuth account (else the hashed guest token) already
    resolved by :func:`padrino.api.human_auth.require_human` — admission is keyed
    on that principal. On a denied decision this raises a 429 whose ``detail`` is
    the structured admission reason; an admitted decision returns the decision so
    the caller can bind its claimed slots to the resulting lobby/member (US-190).
    """
    from padrino.settings import get_settings

    settings = getattr(request.app.state, "auth_settings", None) or get_settings()
    decision = await admit_human(session, settings, principal_id=principal_id, action=action)
    if not decision.allowed:
        raise HTTPException(status_code=_ADMISSION_DENIED_STATUS, detail=decision.reason)
    return decision


class LobbyCreate(BaseModel):
    """Host configuration for a new private lobby (US-147)."""

    model_config = ConfigDict(extra="forbid")

    ruleset_id: Literal["mini7_v1", "bench10_v1"]
    identity_mode: IdentityMode = IdentityMode.ANONYMOUS
    theme_pack_id: str | None = Field(default=None, max_length=64)
    #: Pre-picked human-eligible model agent_build ids assigned to AI seats, in
    #: order. Any AI seat beyond this list is left empty for curated
    #: deterministic auto-fill at launch (US-149). Length must not exceed the AI
    #: seat count (player_count - 1, the host's own HUMAN seat).
    prepick_agent_build_ids: list[uuid.UUID] = Field(default_factory=list)


class LobbySummary(BaseModel):
    """Member-scoped view of a lobby: config + counts-only composition."""

    id: uuid.UUID
    ruleset_id: str
    identity_mode: str
    theme_pack_id: str | None
    stakes: str
    status: str
    invite_token: str
    host_principal_id: uuid.UUID
    league_id: uuid.UUID
    game_id: uuid.UUID | None
    member_count: int
    composition: CompositionSummary


@router.post("/lobbies", response_model=LobbySummary, status_code=status.HTTP_201_CREATED)
async def create_lobby(
    body: LobbyCreate,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Create a private friend lobby owned by the calling human.

    The lobby is OPEN and CASUAL; the host occupies seat 0 (HUMAN). The remaining
    seats are AI: the host's pre-picked human-eligible models fill them in order,
    any beyond the pre-pick are left empty for curated auto-fill (US-149).

    Admission is enforced FIRST: the calling principal's per-user/day game cap,
    inference-$ cap, and the global cost breaker gate lobby creation (US-151). A
    denied decision is a 429 before any row is written.
    """
    admission = await _enforce_admission(
        request, session, principal_id=ctx.principal_id, action=ACTION_CREATE
    )
    ruleset = get_ruleset(body.ruleset_id)
    player_count: int = ruleset.PLAYER_COUNT
    ai_seat_count = player_count - 1

    if len(body.prepick_agent_build_ids) > ai_seat_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="too_many_prepick_models",
        )

    for build_id in body.prepick_agent_build_ids:
        build = await agent_builds_repo.get(session, build_id)
        if build is None or not build.active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown_prepick_model",
            )

    now = datetime.now(UTC)
    league = await leagues_repo.get_or_create_humans_included(session, ruleset_id=body.ruleset_id)

    lobby = await lobbies_repo.create_lobby(
        session,
        ruleset_id=body.ruleset_id,
        identity_mode=body.identity_mode.value,
        theme_pack_id=body.theme_pack_id,
        lobby_seed=secrets.token_hex(16),
        invite_token=secrets.token_urlsafe(24),
        host_principal_id=ctx.principal_id,
        league_id=league.id,
        now=now,
    )

    host_member = await lobbies_repo.add_member(
        session,
        lobby_id=lobby.id,
        principal_id=ctx.principal_id,
        is_host=True,
        now=now,
    )
    # Tie the admission slots to the lobby + host member so an abandoned lobby
    # (idle auto-cancel) releases them — the day caps count games, not attempts.
    await bind_admission_slots(
        session, admission, lobby_id=lobby.id, lobby_member_id=host_member.id
    )

    await lobbies_repo.add_seat(
        session,
        lobby_id=lobby.id,
        seat_index=0,
        seat_kind=LobbySeatKind.HUMAN,
        member_id=host_member.id,
    )
    for offset in range(ai_seat_count):
        prepick = (
            body.prepick_agent_build_ids[offset]
            if offset < len(body.prepick_agent_build_ids)
            else None
        )
        await lobbies_repo.add_seat(
            session,
            lobby_id=lobby.id,
            seat_index=offset + 1,
            seat_kind=LobbySeatKind.AI,
            agent_build_id=prepick,
        )

    await session.commit()
    return await _summary(session, lobby_id=lobby.id)


@router.get("/lobbies/{lobby_id}", response_model=LobbySummary)
async def get_lobby(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Return a member-scoped lobby view with counts-only composition.

    A caller who is not a member of the lobby is rejected (404, so a non-member
    cannot even probe lobby existence). The disclosed composition is counts only
    — never a per-seat human/AI map. A read first reconciles the presence /
    idle-auto-cancel lifecycle (evicts stale members, closes an idle/abandoned
    lobby).
    """
    lobby = await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    await _reconcile_lifecycle(session, lobby)
    summary = await _summary(session, lobby_id=lobby_id)
    await session.commit()
    return summary


class RosterMember(BaseModel):
    """Identity-blind roster entry: no principal id, seat_kind, or human/AI map."""

    member_id: uuid.UUID
    is_host: bool
    ready: bool
    present: bool


class LobbyRoster(BaseModel):
    """A member-scoped roster + counts-only composition (US-148)."""

    id: uuid.UUID
    status: str
    member_count: int
    composition: CompositionSummary
    members: list[RosterMember]


class ReadyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool


@router.post("/lobbies/join/{invite_token}", response_model=LobbySummary)
async def join_lobby(
    invite_token: str,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Join a lobby via its invite link (guest- or account-joinable).

    Single-use-per-person is enforced by membership: re-joining returns the
    existing membership (idempotent), never a duplicate row. Joining a
    LOCKED/LAUNCHED/CLOSED lobby is rejected. The join records presence and
    resets the lobby's idle clock.

    Admission is enforced on a FIRST join (US-151): the calling principal's
    per-user/day join cap, inference-$ cap, and the global cost breaker gate the
    join. An idempotent re-join (already a member) is exempt — it adds no row and
    therefore consumes no slot.
    """
    lobby = await lobbies_repo.get_lobby_by_invite_token(session, invite_token)
    if lobby is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lobby_not_found")

    now = datetime.now(UTC)
    existing = await lobbies_repo.get_member(
        session, lobby_id=lobby.id, principal_id=ctx.principal_id
    )
    if existing is None:
        admission = await _enforce_admission(
            request, session, principal_id=ctx.principal_id, action=ACTION_JOIN
        )
        if lobby.status != LobbyStatus.OPEN.value:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="lobby_not_open")
        member = await lobbies_repo.add_member(
            session,
            lobby_id=lobby.id,
            principal_id=ctx.principal_id,
            is_host=False,
            now=now,
        )
        await bind_admission_slots(session, admission, lobby_id=lobby.id, lobby_member_id=member.id)
        await lobbies_repo.touch_member_presence(session, member_id=member.id, now=now)
    else:
        await lobbies_repo.touch_member_presence(session, member_id=existing.id, now=now)
    await lobbies_repo.touch_lobby(session, lobby_id=lobby.id, now=now)
    summary = await _summary(session, lobby_id=lobby.id)
    await session.commit()
    return summary


@router.get("/lobbies/{lobby_id}/roster", response_model=LobbyRoster)
async def get_roster(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbyRoster:
    """Return the member-scoped, identity-blind roster after reconciling presence."""
    lobby = await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    await _reconcile_lifecycle(session, lobby)
    roster = await _roster(session, lobby_id=lobby_id)
    await session.commit()
    return roster


@router.post("/lobbies/{lobby_id}/ready", response_model=LobbyRoster)
async def set_ready(
    lobby_id: uuid.UUID,
    body: ReadyUpdate,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbyRoster:
    """Set the caller's ready flag; counts as presence."""
    lobby = await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    member = await lobbies_repo.get_member(
        session, lobby_id=lobby_id, principal_id=ctx.principal_id
    )
    assert member is not None
    now = datetime.now(UTC)
    await lobbies_repo.set_member_ready(session, member_id=member.id, ready=body.ready)
    await lobbies_repo.touch_member_presence(session, member_id=member.id, now=now)
    await lobbies_repo.touch_lobby(session, lobby_id=lobby.id, now=now)
    roster = await _roster(session, lobby_id=lobby_id)
    await session.commit()
    return roster


@router.post("/lobbies/{lobby_id}/heartbeat", response_model=LobbyRoster)
async def heartbeat(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbyRoster:
    """Record the caller's presence and reset the lobby idle clock."""
    lobby = await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    member = await lobbies_repo.get_member(
        session, lobby_id=lobby_id, principal_id=ctx.principal_id
    )
    assert member is not None
    now = datetime.now(UTC)
    await lobbies_repo.touch_member_presence(session, member_id=member.id, now=now)
    await lobbies_repo.touch_lobby(session, lobby_id=lobby.id, now=now)
    roster = await _roster(session, lobby_id=lobby_id)
    await session.commit()
    return roster


@router.post("/lobbies/{lobby_id}/lock", response_model=LobbySummary)
async def lock_lobby(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Host-only: lock the roster (OPEN -> LOCKED)."""
    lobby = await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    if lobby.status != LobbyStatus.OPEN.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="lobby_not_open")
    now = datetime.now(UTC)
    await lobbies_repo.set_lobby_status(
        session, lobby_id=lobby_id, status=LobbyStatus.LOCKED.value, now=now
    )
    summary = await _summary(session, lobby_id=lobby_id)
    await session.commit()
    return summary


class LaunchResponse(BaseModel):
    """Result of a launch handoff: the materialized game and lobby status."""

    lobby_id: uuid.UUID
    game_id: uuid.UUID
    status: str
    created: bool


@router.post("/lobbies/{lobby_id}/launch", response_model=LaunchResponse)
async def launch(
    lobby_id: uuid.UUID,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LaunchResponse:
    """Host-only: materialize a real human-lane game from a LOCKED lobby.

    Empty AI seats are filled deterministically from the curated pool, then a
    ``Game`` + ``GameSeat`` rows are written (humans -> ``occupant_principal_id``
    / ``seat_kind=HUMAN``; AI -> ``agent_build_id`` / ``seat_kind=AI``). The
    presence of a HUMAN seat is what hands the game to the human worker lane
    (US-132). Launch is single-fire / idempotent: a second call returns the same
    ``game_id`` (``created=false``) and writes nothing.

    Admission is enforced FIRST (US-151): the host principal's per-user/day game
    cap, inference-$ cap, and the global cost breaker gate the launch. A denied
    decision is a 429 before any game row is materialized.

    Slot lifecycle (US-198): an already-LAUNCHED lobby short-circuits BEFORE
    admission (mirroring join's existing-member guard) so an idempotent re-launch
    claims — and leaks — nothing. On a launch that materializes a NEW game the
    freshly claimed slots are bound to the lobby/host member and the lobby's
    inference reservations are RELEASED (charged ``LlmCall`` spend now drives the
    inference-$ cap, so a held reservation and the charged accounting never
    double-count the same dollars). The lobby's prior create-time slots are
    released first so a created+launched single game consumes exactly ONE
    game-bucket count slot (no create+launch double-count). A failed launch rolls
    the freshly claimed slots back.
    """
    lobby = await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    # Idempotent short-circuit BEFORE admission: an already-launched lobby returns
    # its existing game and claims no slots (a re-launch must not leak budget).
    if lobby.status == LobbyStatus.LAUNCHED.value and lobby.game_id is not None:
        return LaunchResponse(
            lobby_id=lobby_id,
            game_id=lobby.game_id,
            status=LobbyStatus.LAUNCHED.value,
            created=False,
        )

    now = datetime.now(UTC)
    # Release the lobby's create-time slots BEFORE launch admission so the host's
    # own create slot does not block their launch (the create+launch double-count
    # bug): a single created+launched game must consume exactly ONE game-bucket
    # count slot. The materialized game then counts via _principal_games_today, so
    # the per-day cap is preserved. On a failed launch this only frees an attempt
    # slot, which is the intended "caps count games, not attempts" semantics.
    await release_admission_for_lobby(session, lobby_id=lobby_id, released_at=now)

    admission = await _enforce_admission(
        request, session, principal_id=ctx.principal_id, action=ACTION_LAUNCH
    )
    try:
        result = await launch_lobby(session, lobby_id=lobby_id)
    except (LobbyNotLaunchableError, AutoFillPoolError) as exc:
        # Roll the freshly claimed (unbound) slots back so a failed launch leaks
        # neither a count slot nor a held inference reservation.
        await rollback_admission_decision(session, admission)
        await session.commit()
        if isinstance(exc, AutoFillPoolError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="autofill_pool_exhausted"
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc) or "lobby_not_launchable"
        ) from exc

    if result.created:
        # Bind this launch's count slot to the lobby/host member, then release its
        # inference reservations: charged LlmCall spend now governs the
        # inference-$ cap, so a held reservation and the charged accounting never
        # double-count the same dollars.
        host_member = await lobbies_repo.get_member(
            session, lobby_id=lobby_id, principal_id=ctx.principal_id
        )
        await bind_admission_slots(
            session,
            admission,
            lobby_id=lobby_id,
            lobby_member_id=host_member.id if host_member else None,
        )
        await release_inference_reservations_for_lobby(session, lobby_id=lobby_id, released_at=now)
    else:
        # A concurrent caller launched between our guard and now: this call
        # created nothing, so release the slots it claimed.
        await rollback_admission_decision(session, admission)

    await session.commit()
    return LaunchResponse(
        lobby_id=lobby_id,
        game_id=result.game_id,
        status=LobbyStatus.LAUNCHED.value,
        created=result.created,
    )


@router.post("/lobbies/{lobby_id}/kick/{member_id}", response_model=LobbySummary)
async def kick_member(
    lobby_id: uuid.UUID,
    member_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Host-only: remove a member from the lobby."""
    await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    target = await lobbies_repo.get_member_by_id(session, member_id)
    if target is None or target.lobby_id != lobby_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member_not_found")
    if target.is_host:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot_kick_host")
    now = datetime.now(UTC)
    # Release the kicked member's join slot so it does not count against their cap.
    await release_admission_for_member(session, lobby_member_id=member_id, released_at=now)
    await lobbies_repo.remove_member(session, member_id=member_id)
    await lobbies_repo.touch_lobby(session, lobby_id=lobby_id, now=now)
    summary = await _summary(session, lobby_id=lobby_id)
    await session.commit()
    return summary


@router.post("/lobbies/{lobby_id}/transfer/{member_id}", response_model=LobbyRoster)
async def transfer_host(
    lobby_id: uuid.UUID,
    member_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbyRoster:
    """Host-only: hand the host role to another member."""
    await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    target = await lobbies_repo.get_member_by_id(session, member_id)
    if target is None or target.lobby_id != lobby_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member_not_found")
    now = datetime.now(UTC)
    await lobbies_repo.set_host(
        session,
        lobby_id=lobby_id,
        new_host_member_id=member_id,
        new_host_principal_id=target.principal_id,
        now=now,
    )
    roster = await _roster(session, lobby_id=lobby_id)
    await session.commit()
    return roster


@router.get("/lobbies/{lobby_id}/stream")
async def stream_lobby(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> StreamingResponse:
    """Member-scoped lobby state SSE channel (roster/ready/presence, identity-blind).

    Membership is resolved in the route body FIRST so a non-member is a real 404,
    not a half-open stream. The frames are counts-only and carry no per-seat
    human/AI map.
    """
    await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    return StreamingResponse(
        stream_lobby_state(session_factory, lobby_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _require_member_lobby(
    session: AsyncSession, *, lobby_id: uuid.UUID, principal_id: uuid.UUID
) -> Lobby:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    if lobby is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lobby_not_found")
    member = await lobbies_repo.get_member(session, lobby_id=lobby_id, principal_id=principal_id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lobby_not_found")
    return lobby


async def _require_host_lobby(
    session: AsyncSession, *, lobby_id: uuid.UUID, principal_id: uuid.UUID
) -> Lobby:
    lobby = await _require_member_lobby(session, lobby_id=lobby_id, principal_id=principal_id)
    if lobby.host_principal_id != principal_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_host")
    return lobby


async def _reconcile_lifecycle(session: AsyncSession, lobby: Lobby) -> None:
    """Evict stale members and auto-cancel an idle/abandoned non-terminal lobby."""
    if lobby.status not in (LobbyStatus.OPEN.value, LobbyStatus.LOCKED.value):
        return
    now = datetime.now(UTC)
    members = await lobbies_repo.list_members(session, lobby.id)
    if should_auto_cancel(
        lobby_updated_at=_as_aware(lobby.updated_at),
        members=members,
        now=now,
        idle_seconds=_idle_seconds(),
        stale_seconds=_stale_seconds(),
    ):
        await lobbies_repo.set_lobby_status(
            session, lobby_id=lobby.id, status=LobbyStatus.CLOSED.value, now=now
        )
        # An abandoned (never-launched) lobby releases its admission slots so the
        # per-day caps count actual games, not abandoned attempts (US-190).
        await release_admission_for_lobby(session, lobby_id=lobby.id, released_at=now)
        return
    for member_id in stale_member_ids(members, now=now, stale_seconds=_stale_seconds()):
        # The host is never silently evicted (abandonment is handled above by
        # the auto-cancel path); only non-host stale members are dropped.
        member = await lobbies_repo.get_member_by_id(session, member_id)
        if member is not None and not member.is_host:
            await release_admission_for_member(session, lobby_member_id=member_id, released_at=now)
            await lobbies_repo.remove_member(session, member_id=member_id)


def _as_aware(value: datetime) -> datetime:
    """Coerce a SQLite-naive ``DateTime(timezone=True)`` back to UTC-aware."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _summary(session: AsyncSession, *, lobby_id: uuid.UUID) -> LobbySummary:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    assert lobby is not None  # callers resolve the lobby first
    members = await lobbies_repo.list_members(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    composition = composition_summary(
        # A lobby HUMAN seat maps to a game-time HUMAN; an AI seat maps to AI.
        # Counts only — the per-seat layout is never exposed.
        {"seat_kind": seat.seat_kind}
        for seat in seats
    )
    return LobbySummary(
        id=lobby.id,
        ruleset_id=lobby.ruleset_id,
        identity_mode=lobby.identity_mode,
        theme_pack_id=lobby.theme_pack_id,
        stakes=LobbyStakes(lobby.stakes).value,
        status=lobby.status,
        invite_token=lobby.invite_token,
        host_principal_id=lobby.host_principal_id,
        league_id=lobby.league_id,
        game_id=lobby.game_id,
        member_count=len(members),
        composition=composition,
    )


async def _roster(session: AsyncSession, *, lobby_id: uuid.UUID) -> LobbyRoster:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    assert lobby is not None
    members = await lobbies_repo.list_members(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    composition = composition_summary({"seat_kind": seat.seat_kind} for seat in seats)
    now = datetime.now(UTC)
    views: list[MemberView] = roster_view(members, now=now, stale_seconds=_stale_seconds())
    return LobbyRoster(
        id=lobby.id,
        status=lobby.status,
        member_count=len(members),
        composition=composition,
        members=[
            RosterMember(
                member_id=v.member_id,
                is_host=v.is_host,
                ready=v.ready,
                present=v.present,
            )
            for v in views
        ],
    )


__all__ = [
    "LaunchResponse",
    "LobbyCreate",
    "LobbyRoster",
    "LobbySummary",
    "ReadyUpdate",
    "RosterMember",
    "router",
]
