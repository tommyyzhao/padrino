"""Private friend lobby create/configure routes (US-147).

A host creates a private lobby and configures the human-multiplayer game it will
launch: the ruleset/size, the per-game
``identity_mode`` (default ANONYMOUS), a static ``theme_pack_id``, the bot
canonical model-slug selections vs curated auto-fill, and stakes pinned
``CASUAL``. ``GET /lobbies/{id}`` returns a member-scoped view whose composition
is COUNTS ONLY — never a per-seat human/AI map — produced by the single canonical
:func:`padrino.core.composition.composition_summary` (US-126/US-142).

There is no public matchmaking in v1 (decision 2): a lobby is reachable only by
its members (and, in US-148, by an invite link).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.deps import get_session, get_session_factory
from padrino.api.human_auth import HumanPrincipalContext, require_human
from padrino.api.human_consent import client_ip_hash
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
from padrino.api.lobby_readiness import evaluate_lobby_rows
from padrino.api.lobby_state import stream_lobby_state
from padrino.core.cadence import CASUAL_STANDARD_CADENCE_ID
from padrino.core.composition import CompositionSummary, composition_summary
from padrino.core.enums import IdentityMode, LobbySeatKind, LobbyStakes, LobbyStatus
from padrino.core.lobby import LobbySeatPlanError
from padrino.core.lobby.seating import LobbySeatPlanEntry
from padrino.core.rulesets import Ruleset, get_ruleset
from padrino.core.rulesets.custom_deck import (
    CustomRoleDeckReason,
    validate_custom_role_deck,
)
from padrino.core.rulesets.snapshot import (
    BUILTIN_CADENCE_ID,
    SUPPORTED_CADENCE_IDS,
    GameRulesSnapshot,
    canonical_rules_dict,
    canonical_rules_hash,
    snapshot_from_ruleset,
)
from padrino.db.models import Lobby, LobbyMember, LobbySeat
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import lobbies as lobbies_repo
from padrino.db.rules_snapshots import runtime_ruleset_from_pair
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
from padrino.openrouter.funding_preflight import (
    FundingPreflightError,
    FundingQuote,
    build_lobby_funding_quote,
)
from padrino.openrouter.lobby_model_selection import (
    LobbyModelSelectionError,
    RequestedAiModelSelection,
    RequestedAutoFillPolicy,
    resolve_lobby_model_selections,
    selection_json,
)
from padrino.settings import Settings

router = APIRouter()


def _stale_seconds() -> float:
    from padrino.settings import get_settings

    return get_settings().padrino_lobby_presence_stale_seconds


def _idle_seconds() -> float:
    from padrino.settings import get_settings

    return get_settings().padrino_lobby_idle_cancel_seconds


def ranked_human_rating_enabled_from_request(request: Request) -> bool:
    """Read the ranked human-rating kill-switch from request-scoped settings."""
    from padrino.settings import get_settings

    settings = getattr(request.app.state, "auth_settings", None) or get_settings()
    return getattr(settings, "padrino_ranked_human_rating_enabled", False) is True


def _openrouter_default_zdr() -> bool:
    """Resolve the server-owned casual router privacy default."""
    from padrino.settings import get_settings

    return get_settings().padrino_openrouter_default_zdr


def _funding_settings(request: Request) -> Settings:
    """Return the request-scoped deployment policy for funding admission."""
    from padrino.settings import get_settings

    settings = getattr(request.app.state, "auth_settings", None) or get_settings()
    if not isinstance(settings, Settings):
        return get_settings()
    return settings


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
    """Host configuration for a casual custom private lobby (US-337)."""

    model_config = ConfigDict(extra="forbid")

    built_in_ruleset_id: str | None = Field(default=None, min_length=1)
    custom_role_counts: dict[str, int] | None = None
    #: Backward-compatible alias for pre-US-337 clients. New clients should send
    #: ``built_in_ruleset_id`` or ``custom_role_counts``.
    ruleset_id: str | None = Field(default=None, min_length=1)
    cadence_preset: str = CASUAL_STANDARD_CADENCE_ID
    human_seat_count: int | None = Field(default=None, ge=1)
    identity_mode: IdentityMode = IdentityMode.ANONYMOUS
    theme_pack_id: str | None = Field(default=None, max_length=64)
    #: Canonical OpenRouter model choices keyed to configured AI slots.  These
    #: are server-resolved against one immutable catalog snapshot; player-facing
    #: AgentBuild UUIDs are deliberately not accepted (US-348).
    ai_model_selections: list[RequestedAiModelSelection] = Field(default_factory=list)
    #: Deterministically fills every AI slot not explicitly selected above.
    autofill_policy: RequestedAutoFillPolicy | None = None
    #: Deprecated US-291 checkbox. Ranked invite lobbies are no longer reachable
    #: through this custom/casual route.
    ranked: bool | None = None
    integrity_acknowledged: bool | None = None


class LobbyConfigUpdate(BaseModel):
    """Host-only mutable lobby configuration before the roster freezes."""

    model_config = ConfigDict(extra="forbid")

    built_in_ruleset_id: str | None = Field(default=None, min_length=1)
    custom_role_counts: dict[str, int] | None = None
    ruleset_id: str | None = Field(default=None, min_length=1)
    cadence_preset: str | None = None
    human_seat_count: int | None = Field(default=None, ge=1)
    identity_mode: IdentityMode | None = None
    theme_pack_id: str | None = Field(default=None, max_length=64)
    ai_model_selections: list[RequestedAiModelSelection] | None = None
    autofill_policy: RequestedAutoFillPolicy | None = None
    ranked: bool | None = None
    integrity_acknowledged: bool | None = None


class RoleCountSummary(BaseModel):
    """One normalized role-count row for API rules summaries."""

    role: str
    count: int


class RulesSummary(BaseModel):
    """Rules snapshot summary safe to return to lobby members."""

    ruleset_id: str
    built_in_ruleset_id: str | None
    custom: bool
    player_count: int
    cadence_preset: str
    role_counts: list[RoleCountSummary]
    validation_errors: list[str] = Field(default_factory=list)


class LobbyAiRosterCard(BaseModel):
    """One pre-game AI model card, deliberately detached from a seat identity."""

    canonical_slug: str
    count: int


class LobbySummary(BaseModel):
    """Member-scoped view of a lobby: config + counts-only composition."""

    id: uuid.UUID
    ruleset_id: str
    rules_summary: RulesSummary
    identity_mode: str
    theme_pack_id: str | None
    stakes: str
    ranked: bool
    integrity_acknowledged: bool
    human_seat_capacity: int
    ai_seat_count: int
    status: str
    invite_token: str
    league_id: uuid.UUID
    game_id: uuid.UUID | None
    member_count: int
    composition: CompositionSummary
    #: A named pre-game model roster without seat indexes, builds, or routing
    #: metadata.  In-game public identities remain seeded and undisclosed.
    ai_roster: list[LobbyAiRosterCard] = Field(default_factory=list)


def _ruleset_or_422(ruleset_id: str) -> Ruleset:
    try:
        return get_ruleset(ruleset_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unknown_ruleset",
        ) from exc


@dataclass(frozen=True, slots=True)
class _ResolvedLobbyRules:
    ruleset_id: str
    ruleset: Any
    rules_snapshot_json: dict[str, object]
    rules_snapshot_hash: str
    summary: RulesSummary
    legacy_ruleset_alias: bool


def _resolve_rules_for_create(body: LobbyCreate) -> _ResolvedLobbyRules:
    cadence_preset = body.cadence_preset
    if body.ruleset_id is not None and "cadence_preset" not in body.model_fields_set:
        cadence_preset = BUILTIN_CADENCE_ID
    return _resolve_rules(
        built_in_ruleset_id=body.built_in_ruleset_id,
        custom_role_counts=body.custom_role_counts,
        legacy_ruleset_id=body.ruleset_id,
        cadence_preset=cadence_preset,
    )


def _resolve_rules_for_update(
    body: LobbyConfigUpdate,
    *,
    lobby: Lobby,
) -> _ResolvedLobbyRules:
    if (
        body.built_in_ruleset_id is None
        and body.custom_role_counts is None
        and body.ruleset_id is None
    ):
        if body.cadence_preset is not None and body.cadence_preset not in SUPPORTED_CADENCE_IDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown_cadence_preset",
            )
        current = _runtime_rules_for_lobby(lobby)
        if body.cadence_preset is None:
            return current
        return _resolved_rules_with_cadence(
            lobby=lobby,
            current=current,
            cadence_id=body.cadence_preset,
        )
    return _resolve_rules(
        built_in_ruleset_id=body.built_in_ruleset_id,
        custom_role_counts=body.custom_role_counts,
        legacy_ruleset_id=body.ruleset_id,
        cadence_preset=body.cadence_preset or BUILTIN_CADENCE_ID,
    )


def _runtime_rules_for_lobby(lobby: Lobby) -> _ResolvedLobbyRules:
    ruleset = runtime_ruleset_from_pair(
        ruleset_id=lobby.ruleset_id,
        rules_snapshot_json=lobby.rules_snapshot_json,
        rules_snapshot_hash=lobby.rules_snapshot_hash,
    )
    snapshot = (
        GameRulesSnapshot.model_validate(lobby.rules_snapshot_json)
        if lobby.rules_snapshot_json is not None
        else snapshot_from_ruleset(ruleset)
    )
    body = canonical_rules_dict(snapshot)
    snapshot_hash = canonical_rules_hash(snapshot)
    return _ResolvedLobbyRules(
        ruleset_id=lobby.ruleset_id,
        ruleset=ruleset,
        rules_snapshot_json=body,
        rules_snapshot_hash=snapshot_hash,
        summary=_rules_summary(
            ruleset_id=lobby.ruleset_id,
            built_in_ruleset_id=None
            if lobby.ruleset_id.startswith("custom:")
            else lobby.ruleset_id,
            custom=lobby.ruleset_id.startswith("custom:"),
            snapshot=snapshot,
        ),
        legacy_ruleset_alias=False,
    )


def _resolved_rules_with_cadence(
    *,
    lobby: Lobby,
    current: _ResolvedLobbyRules,
    cadence_id: str,
) -> _ResolvedLobbyRules:
    custom = lobby.ruleset_id.startswith("custom:")
    if custom:
        snapshot = GameRulesSnapshot.model_validate(current.rules_snapshot_json).model_copy(
            update={"cadence_id": cadence_id}
        )
        ruleset_id = snapshot.RULESET_ID
        ruleset: Any = snapshot
    else:
        snapshot = snapshot_from_ruleset(current.ruleset, cadence_id=cadence_id)
        ruleset_id = lobby.ruleset_id
        ruleset = current.ruleset
    return _ResolvedLobbyRules(
        ruleset_id=ruleset_id,
        ruleset=ruleset,
        rules_snapshot_json=canonical_rules_dict(snapshot),
        rules_snapshot_hash=canonical_rules_hash(snapshot),
        summary=_rules_summary(
            ruleset_id=ruleset_id,
            built_in_ruleset_id=None if custom else lobby.ruleset_id,
            custom=custom,
            snapshot=snapshot,
        ),
        legacy_ruleset_alias=False,
    )


def _resolve_rules(
    *,
    built_in_ruleset_id: str | None,
    custom_role_counts: Mapping[str, int] | None,
    legacy_ruleset_id: str | None,
    cadence_preset: str,
) -> _ResolvedLobbyRules:
    if cadence_preset not in SUPPORTED_CADENCE_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unknown_cadence_preset",
        )
    sources = [
        built_in_ruleset_id is not None,
        custom_role_counts is not None,
        legacy_ruleset_id is not None,
    ]
    if sum(sources) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="one_rules_source_required",
        )

    if custom_role_counts is not None:
        validation = validate_custom_role_deck(custom_role_counts)
        if not validation.ok or validation.snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "validation_errors": _reason_values(validation.reasons),
                    "rules_summary": _invalid_custom_rules_summary(custom_role_counts),
                },
            )
        snapshot = validation.snapshot.model_copy(update={"cadence_id": cadence_preset})
        snapshot_body = canonical_rules_dict(snapshot)
        snapshot_hash = canonical_rules_hash(snapshot)
        ruleset_id = snapshot.RULESET_ID
        return _ResolvedLobbyRules(
            ruleset_id=ruleset_id,
            ruleset=snapshot,
            rules_snapshot_json=snapshot_body,
            rules_snapshot_hash=snapshot_hash,
            summary=_rules_summary(
                ruleset_id=ruleset_id,
                built_in_ruleset_id=None,
                custom=True,
                snapshot=snapshot,
            ),
            legacy_ruleset_alias=False,
        )

    registered_id = built_in_ruleset_id or legacy_ruleset_id
    assert registered_id is not None
    ruleset = _ruleset_or_422(registered_id)
    snapshot = snapshot_from_ruleset(ruleset, cadence_id=cadence_preset)
    return _ResolvedLobbyRules(
        ruleset_id=ruleset.RULESET_ID,
        ruleset=ruleset,
        rules_snapshot_json=canonical_rules_dict(snapshot),
        rules_snapshot_hash=canonical_rules_hash(snapshot),
        summary=_rules_summary(
            ruleset_id=ruleset.RULESET_ID,
            built_in_ruleset_id=ruleset.RULESET_ID,
            custom=False,
            snapshot=snapshot,
        ),
        legacy_ruleset_alias=legacy_ruleset_id is not None,
    )


def _rules_summary(
    *,
    ruleset_id: str,
    built_in_ruleset_id: str | None,
    custom: bool,
    snapshot: GameRulesSnapshot,
    validation_errors: list[str] | None = None,
) -> RulesSummary:
    return RulesSummary(
        ruleset_id=ruleset_id,
        built_in_ruleset_id=built_in_ruleset_id,
        custom=custom,
        player_count=snapshot.PLAYER_COUNT,
        cadence_preset=snapshot.cadence_id,
        role_counts=[
            RoleCountSummary(role=entry.role.value, count=entry.count)
            for entry in sorted(snapshot.role_counts, key=lambda item: item.role.value)
        ],
        validation_errors=validation_errors or [],
    )


def _invalid_custom_rules_summary(role_counts: Mapping[str, int]) -> dict[str, object]:
    normalized: dict[str, int] = {}
    for role, count in role_counts.items():
        normalized[role] = normalized.get(role, 0) + count
    return {
        "custom": True,
        "player_count": sum(count for count in normalized.values() if count > 0),
        "role_counts": [{"role": role, "count": normalized[role]} for role in sorted(normalized)],
    }


def _reason_values(reasons: tuple[CustomRoleDeckReason, ...]) -> list[str]:
    return [reason.value for reason in reasons]


def _human_capacity(body_value: int | None, *, player_count: int, legacy_alias: bool) -> int:
    capacity = player_count if body_value is None and legacy_alias else body_value or 1
    if capacity < 1 or capacity > player_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_human_seat_count",
        )
    return capacity


def _ai_seat_count(*, player_count: int, human_seat_capacity: int) -> int:
    return player_count - human_seat_capacity


def _ensure_casual_create(body: LobbyCreate | LobbyConfigUpdate) -> None:
    if body.ranked is True or body.integrity_acknowledged is True:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ranked_lobbies_deprecated",
        )


def _configured_seats(
    *,
    player_count: int,
    human_seat_capacity: int,
    host_member_id: uuid.UUID,
    legacy_agent_build_ids_by_index: Mapping[int, uuid.UUID] | None = None,
) -> list[LobbySeatPlanEntry]:
    legacy_builds = legacy_agent_build_ids_by_index or {}
    return [
        LobbySeatPlanEntry(
            seat_index=seat_index,
            seat_kind=LobbySeatKind.HUMAN if seat_index == 0 else LobbySeatKind.AI,
            member_id=str(host_member_id) if seat_index == 0 else None,
            agent_build_id=(
                str(legacy_builds[seat_index]) if seat_index in legacy_builds else None
            ),
        )
        for seat_index in range(player_count)
    ]


@router.post("/lobbies", response_model=LobbySummary, status_code=status.HTTP_201_CREATED)
async def create_lobby(
    body: LobbyCreate,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Create a private friend lobby owned by the calling human.

    The lobby is OPEN and defaults casual; the host occupies seat 0 (HUMAN).
    Canonical model selections, when supplied, are frozen against one catalog
    snapshot; legacy lobbies without a selection keep the existing curated-build
    launch compatibility path.

    Admission is enforced FIRST: the calling principal's per-user/day game cap,
    inference-$ cap, and the global cost breaker gate lobby creation (US-151). A
    denied decision is a 429 before any row is written.
    """
    _ensure_casual_create(body)
    resolved_rules = _resolve_rules_for_create(body)
    now = datetime.now(UTC)
    admission = await _enforce_admission(
        request, session, principal_id=ctx.principal_id, action=ACTION_CREATE
    )
    player_count = resolved_rules.ruleset.PLAYER_COUNT
    human_seat_capacity = _human_capacity(
        body.human_seat_count,
        player_count=player_count,
        legacy_alias=resolved_rules.legacy_ruleset_alias,
    )
    lobby_seed = secrets.token_hex(16)
    model_selections_by_index: dict[int, dict[str, object]] = {}
    if body.ai_model_selections or body.autofill_policy is not None:
        try:
            resolved_models = await resolve_lobby_model_selections(
                session,
                ruleset_id=resolved_rules.ruleset_id,
                principal_id=ctx.principal_id,
                ai_seat_indices=tuple(range(human_seat_capacity, player_count)),
                requested_selections=body.ai_model_selections,
                autofill_policy=body.autofill_policy,
                default_zdr=_openrouter_default_zdr(),
                lobby_seed=lobby_seed,
            )
        except LobbyModelSelectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        model_selections_by_index = {
            seat_index: selection_json(selection)
            for seat_index, selection in resolved_models.selections_by_seat.items()
        }

    league = await leagues_repo.get_or_create_humans_included(
        session, ruleset_id=resolved_rules.ruleset_id, ranked=False
    )

    lobby = await lobbies_repo.create_lobby(
        session,
        ruleset_id=resolved_rules.ruleset_id,
        identity_mode=body.identity_mode.value,
        theme_pack_id=body.theme_pack_id,
        lobby_seed=lobby_seed,
        invite_token=secrets.token_urlsafe(24),
        integrity_acknowledged=False,
        host_principal_id=ctx.principal_id,
        league_id=league.id,
        now=now,
        human_seat_capacity=human_seat_capacity,
        rules_snapshot_json=resolved_rules.rules_snapshot_json,
        rules_snapshot_hash=resolved_rules.rules_snapshot_hash,
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

    for seat in _configured_seats(
        player_count=player_count,
        human_seat_capacity=human_seat_capacity,
        host_member_id=host_member.id,
    ):
        await lobbies_repo.add_seat(
            session,
            lobby_id=lobby.id,
            seat_index=seat.seat_index,
            seat_kind=LobbySeatKind(seat.seat_kind),
            member_id=uuid.UUID(seat.member_id) if seat.member_id is not None else None,
            agent_build_id=(
                uuid.UUID(seat.agent_build_id) if seat.agent_build_id is not None else None
            ),
            model_selection=model_selections_by_index.get(seat.seat_index),
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


@router.patch("/lobbies/{lobby_id}/configuration", response_model=LobbySummary)
async def update_lobby_configuration(
    lobby_id: uuid.UUID,
    body: LobbyConfigUpdate,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Host-only mutable lobby configuration.

    Rules/identity/capacity/model choices freeze after the first non-host joins.
    Theme remains safe while the lobby is still OPEN; LOCKED freezes everything.
    """
    _ensure_casual_create(body)
    lobby = await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    if lobby.status != LobbyStatus.OPEN.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="lobby_config_locked")

    fields = body.model_fields_set
    structural_fields = fields - {"theme_pack_id"}
    members = await lobbies_repo.list_members(session, lobby_id)
    has_non_host_member = any(not member.is_host for member in members)
    now = datetime.now(UTC)
    if structural_fields and has_non_host_member:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="lobby_config_frozen_after_join",
        )

    if not structural_fields:
        if "theme_pack_id" not in fields:
            summary = await _summary(session, lobby_id=lobby_id)
            await session.commit()
            return summary
        await lobbies_repo.update_theme(
            session,
            lobby_id=lobby_id,
            theme_pack_id=body.theme_pack_id,
            now=now,
        )
        summary = await _summary(session, lobby_id=lobby_id)
        await session.commit()
        return summary

    resolved_rules = _resolve_rules_for_update(body, lobby=lobby)
    player_count = resolved_rules.ruleset.PLAYER_COUNT
    existing_capacity = _effective_human_capacity(lobby, player_count=player_count)
    human_seat_capacity = _human_capacity(
        body.human_seat_count if body.human_seat_count is not None else existing_capacity,
        player_count=player_count,
        legacy_alias=False,
    )
    existing_seats = await lobbies_repo.list_seats(session, lobby_id)
    legacy_agent_build_ids_by_index: dict[int, uuid.UUID] = {}
    model_selections_by_index: dict[int, dict[str, object]] = {}
    selection_fields = {"ai_model_selections", "autofill_policy"}
    if fields & selection_fields:
        try:
            resolved_models = await resolve_lobby_model_selections(
                session,
                ruleset_id=resolved_rules.ruleset_id,
                principal_id=ctx.principal_id,
                ai_seat_indices=tuple(range(human_seat_capacity, player_count)),
                requested_selections=body.ai_model_selections or [],
                autofill_policy=body.autofill_policy,
                default_zdr=_openrouter_default_zdr(),
                lobby_seed=lobby.lobby_seed,
            )
        except LobbyModelSelectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        model_selections_by_index = {
            seat_index: selection_json(selection)
            for seat_index, selection in resolved_models.selections_by_seat.items()
        }
    else:
        for seat in existing_seats:
            if seat.seat_index < human_seat_capacity:
                continue
            if seat.agent_build_id is not None:
                legacy_agent_build_ids_by_index[seat.seat_index] = seat.agent_build_id
            if seat.model_selection is not None:
                model_selections_by_index[seat.seat_index] = seat.model_selection
    host_member = next(member for member in members if member.is_host)
    updated = await lobbies_repo.update_configuration(
        session,
        lobby_id=lobby_id,
        ruleset_id=resolved_rules.ruleset_id,
        identity_mode=(body.identity_mode or IdentityMode(lobby.identity_mode)).value,
        theme_pack_id=body.theme_pack_id if "theme_pack_id" in fields else lobby.theme_pack_id,
        human_seat_capacity=human_seat_capacity,
        rules_snapshot_json=resolved_rules.rules_snapshot_json,
        rules_snapshot_hash=resolved_rules.rules_snapshot_hash,
        now=now,
    )
    assert updated is not None
    await lobbies_repo.replace_seats(
        session,
        lobby_id=lobby_id,
        seats=_configured_seats(
            player_count=player_count,
            human_seat_capacity=human_seat_capacity,
            host_member_id=host_member.id,
            legacy_agent_build_ids_by_index=legacy_agent_build_ids_by_index,
        ),
        model_selections=model_selections_by_index,
    )
    summary = await _summary(session, lobby_id=lobby_id)
    await session.commit()
    return summary


class RosterMember(BaseModel):
    """Caller-safe roster entry: no principal id, seat_kind, or human/AI map."""

    member_id: uuid.UUID
    display_label: str
    is_self: bool
    host: bool
    ready: bool
    present: bool


class LobbyLaunchReadiness(BaseModel):
    """Safe, server-derived reasons that a custom table cannot begin yet."""

    allowed: bool
    reasons: list[str]


class LobbyRoster(BaseModel):
    """A member-scoped roster + counts-only composition (US-148)."""

    id: uuid.UUID
    status: str
    member_count: int
    composition: CompositionSummary
    members: list[RosterMember]
    launch_readiness: LobbyLaunchReadiness


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
        if lobby.status != LobbyStatus.OPEN.value:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="lobby_not_open")
        admission = await _enforce_admission(
            request, session, principal_id=ctx.principal_id, action=ACTION_JOIN
        )
        try:
            member = await lobbies_repo.add_member(
                session,
                lobby_id=lobby.id,
                principal_id=ctx.principal_id,
                is_host=False,
                now=now,
            )
            await lobbies_repo.claim_member_seat(session, lobby_id=lobby.id, member_id=member.id)
            await bind_admission_slots(
                session, admission, lobby_id=lobby.id, lobby_member_id=member.id
            )
            await lobbies_repo.touch_member_presence(session, member_id=member.id, now=now)
        except LobbySeatPlanError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=exc.reason.value
            ) from exc
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
    roster = await _roster(session, lobby_id=lobby_id, caller_principal_id=ctx.principal_id)
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
    roster = await _roster(session, lobby_id=lobby_id, caller_principal_id=ctx.principal_id)
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
    roster = await _roster(session, lobby_id=lobby_id, caller_principal_id=ctx.principal_id)
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
    await _assert_lobby_readiness(
        session,
        lobby=lobby,
        now=now,
        available_ai_build_count=None,
    )
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


class LaunchFundingConfirmation(BaseModel):
    """Client acknowledgement of the exact frozen funding quote it reviewed."""

    model_config = ConfigDict(extra="forbid")

    funding_quote_hash: str | None = Field(default=None, min_length=64, max_length=64)
    #: BYOK never reserves promo; the host must instead confirm this maximum
    #: before launch. It is bound into the quote hash and cannot be replayed
    #: against a changed model/rules/catalog snapshot.
    byok_max_estimate_micros: int | None = Field(default=None, ge=0)


class FundingPreflightResponse(BaseModel):
    """Safe, secret-free quote details for the custom-lobby launch UI."""

    funded: bool
    allowed: bool
    funding_source: str | None
    reason: str
    reasons: list[str]
    reservation_micros: int | None
    expected_total_micros: int | None
    funding_quote_hash: str | None
    estimate_hash: str | None
    model_hash: str | None
    catalog_hash: str | None
    rules_hash: str | None
    funding_hash: str | None
    byok_max_estimate_micros: int | None


def _funding_preflight_response(quote: FundingQuote) -> FundingPreflightResponse:
    """Project a quote without any credential or principal reference."""
    return FundingPreflightResponse(
        funded=quote.funded,
        allowed=quote.allowed,
        funding_source=quote.source.value if quote.source is not None else None,
        reason=quote.reason,
        reasons=list(quote.reasons),
        reservation_micros=quote.reservation_micros,
        expected_total_micros=quote.expected_total_micros,
        funding_quote_hash=quote.funding_quote_hash,
        estimate_hash=quote.estimate_hash,
        model_hash=quote.model_hash,
        catalog_hash=quote.catalog_hash,
        rules_hash=quote.rules_hash,
        funding_hash=quote.funding_hash,
        byok_max_estimate_micros=quote.byok_max_estimate_micros,
    )


@router.post("/lobbies/{lobby_id}/funding-preflight", response_model=FundingPreflightResponse)
async def funding_preflight(
    lobby_id: uuid.UUID,
    request: Request,
    body: LaunchFundingConfirmation | None = None,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> FundingPreflightResponse:
    """Quote frozen custom-lobby funding without creating a reservation.

    The host may call this while a lobby is still mutable or once it is locked.
    Launch recomputes it under the wallet-owner lock, so this response is a
    confirmation contract rather than an authority to spend credit.
    """
    lobby = await _require_host_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    try:
        quote = await build_lobby_funding_quote(
            session,
            lobby=lobby,
            settings=_funding_settings(request),
            byok_max_estimate_micros=(body.byok_max_estimate_micros if body is not None else None),
        )
    except FundingPreflightError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    return _funding_preflight_response(quote)


@router.post("/lobbies/{lobby_id}/launch", response_model=LaunchResponse)
async def launch(
    lobby_id: uuid.UUID,
    request: Request,
    body: LaunchFundingConfirmation | None = None,
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
        league = await leagues_repo.get(session, lobby.league_id)
        source_ip_hash = client_ip_hash(request) if league is not None and league.ranked else None
        result = await launch_lobby(
            session,
            lobby_id=lobby_id,
            source_ip_hash=source_ip_hash,
            now=now,
            stale_seconds=_stale_seconds(),
            funding_quote_hash=body.funding_quote_hash if body is not None else None,
            byok_max_estimate_micros=(body.byok_max_estimate_micros if body is not None else None),
            funding_settings=_funding_settings(request),
        )
    except (LobbyNotLaunchableError, AutoFillPoolError, FundingPreflightError) as exc:
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
    now = datetime.now(UTC)
    if target is None:
        summary = await _summary(session, lobby_id=lobby_id)
        await session.commit()
        return summary
    if target.lobby_id != lobby_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member_not_found")
    if target.is_host:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot_kick_host")
    await _release_and_remove_member(session, lobby_id=lobby_id, member_id=member_id, now=now)
    await lobbies_repo.touch_lobby(session, lobby_id=lobby_id, now=now)
    summary = await _summary(session, lobby_id=lobby_id)
    await session.commit()
    return summary


@router.post("/lobbies/{lobby_id}/leave", response_model=LobbySummary)
async def leave_lobby(
    lobby_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Member-only: leave a lobby and release the caller's reserved seat."""
    await _require_member_lobby(session, lobby_id=lobby_id, principal_id=ctx.principal_id)
    member = await lobbies_repo.get_member(
        session, lobby_id=lobby_id, principal_id=ctx.principal_id
    )
    assert member is not None
    was_host = member.is_host
    if was_host:
        members = await lobbies_repo.list_members(session, lobby_id)
        if any(candidate.id != member.id for candidate in members):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="host_must_transfer_before_leave",
            )

    now = datetime.now(UTC)
    await _release_and_remove_member(session, lobby_id=lobby_id, member_id=member.id, now=now)
    if was_host:
        await lobbies_repo.set_lobby_status(
            session, lobby_id=lobby_id, status=LobbyStatus.CLOSED.value, now=now
        )
    else:
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
    roster = await _roster(session, lobby_id=lobby_id, caller_principal_id=ctx.principal_id)
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
        stream_lobby_state(session_factory, lobby_id, caller_principal_id=ctx.principal_id),
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
            await _release_and_remove_member(
                session, lobby_id=lobby.id, member_id=member_id, now=now
            )


async def _release_and_remove_member(
    session: AsyncSession, *, lobby_id: uuid.UUID, member_id: uuid.UUID, now: datetime
) -> None:
    """Release one member's lobby seat and admissions before deleting the member."""
    await lobbies_repo.release_member_seat(session, lobby_id=lobby_id, member_id=member_id)
    await release_admission_for_member(session, lobby_member_id=member_id, released_at=now)
    await lobbies_repo.remove_member(session, member_id=member_id)


async def _assert_lobby_readiness(
    session: AsyncSession,
    *,
    lobby: Lobby,
    now: datetime,
    available_ai_build_count: int | None,
) -> None:
    """Fail closed when lobby rows cannot safely advance toward launch."""
    ruleset = _runtime_ruleset_for_lobby(lobby)
    decision = evaluate_lobby_rows(
        members=await lobbies_repo.list_members(session, lobby.id),
        seats=await lobbies_repo.list_seats(session, lobby.id),
        expected_player_count=ruleset.PLAYER_COUNT,
        available_ai_build_count=available_ai_build_count,
        now=now,
        stale_seconds=_stale_seconds(),
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=decision.reasons[0].value,
        )


def _as_aware(value: datetime) -> datetime:
    """Coerce a SQLite-naive ``DateTime(timezone=True)`` back to UTC-aware."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _runtime_ruleset_for_lobby(lobby: Lobby) -> Ruleset:
    try:
        return runtime_ruleset_from_pair(
            ruleset_id=lobby.ruleset_id,
            rules_snapshot_json=lobby.rules_snapshot_json,
            rules_snapshot_hash=lobby.rules_snapshot_hash,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc) or "invalid_rules_snapshot",
        ) from exc


def _effective_human_capacity(lobby: Lobby, *, player_count: int) -> int:
    if lobby.human_seat_capacity is None:
        return player_count
    return lobby.human_seat_capacity


async def _summary(session: AsyncSession, *, lobby_id: uuid.UUID) -> LobbySummary:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    assert lobby is not None  # callers resolve the lobby first
    members = await lobbies_repo.list_members(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    league = await leagues_repo.get(session, lobby.league_id)
    resolved_rules = _runtime_rules_for_lobby(lobby)
    human_seat_capacity = _effective_human_capacity(
        lobby, player_count=resolved_rules.ruleset.PLAYER_COUNT
    )
    composition = composition_summary(
        # A lobby HUMAN seat maps to a game-time HUMAN; an AI seat maps to AI.
        # Counts only — the per-seat layout is never exposed.
        {"seat_kind": seat.seat_kind}
        for seat in seats
    )
    return LobbySummary(
        id=lobby.id,
        ruleset_id=lobby.ruleset_id,
        rules_summary=resolved_rules.summary,
        identity_mode=lobby.identity_mode,
        theme_pack_id=lobby.theme_pack_id,
        stakes=LobbyStakes(lobby.stakes).value,
        ranked=league.ranked if league is not None else False,
        integrity_acknowledged=lobby.integrity_acknowledged,
        human_seat_capacity=human_seat_capacity,
        ai_seat_count=_ai_seat_count(
            player_count=resolved_rules.ruleset.PLAYER_COUNT,
            human_seat_capacity=human_seat_capacity,
        ),
        status=lobby.status,
        invite_token=lobby.invite_token,
        league_id=lobby.league_id,
        game_id=lobby.game_id,
        member_count=len(members),
        composition=composition,
        ai_roster=_ai_roster_cards(seats),
    )


async def _roster(
    session: AsyncSession, *, lobby_id: uuid.UUID, caller_principal_id: uuid.UUID
) -> LobbyRoster:
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    assert lobby is not None
    members = await lobbies_repo.list_members(session, lobby_id)
    display_names = await lobbies_repo.list_member_display_names(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    composition = composition_summary({"seat_kind": seat.seat_kind} for seat in seats)
    now = datetime.now(UTC)
    views: list[MemberView] = roster_view(
        members,
        display_names_by_member_id=display_names,
        caller_principal_id=caller_principal_id,
        now=now,
        stale_seconds=_stale_seconds(),
    )
    readiness = _launch_readiness(
        lobby=lobby,
        members=members,
        seats=seats,
        now=now,
    )
    return LobbyRoster(
        id=lobby.id,
        status=lobby.status,
        member_count=len(members),
        composition=composition,
        members=[
            RosterMember(
                member_id=v.member_id,
                display_label=v.display_label,
                is_self=v.is_self,
                host=v.host,
                ready=v.ready,
                present=v.present,
            )
            for v in views
        ],
        launch_readiness=readiness,
    )


def _ai_roster_cards(seats: list[LobbySeat]) -> list[LobbyAiRosterCard]:
    """Project frozen selections as named cards without disclosing seat bindings.

    ``model_selection`` also contains immutable catalog, prompt, routing, and
    funding-owner facts.  The waiting room needs only the selected canonical
    model label and its multiplicity; malformed legacy rows are omitted rather
    than forwarding arbitrary JSON into a browser response.
    """
    counts: dict[str, int] = {}
    for seat in seats:
        if seat.seat_kind != LobbySeatKind.AI.value or not isinstance(seat.model_selection, dict):
            continue
        raw_slug = seat.model_selection.get("canonical_slug")
        if not isinstance(raw_slug, str):
            continue
        slug = raw_slug.strip().lower()
        if not slug or len(slug) > 512:
            continue
        counts[slug] = counts.get(slug, 0) + 1
    return [
        LobbyAiRosterCard(canonical_slug=slug, count=count)
        for slug, count in sorted(counts.items())
    ]


def _launch_readiness(
    *,
    lobby: Lobby,
    members: list[LobbyMember],
    seats: list[LobbySeat],
    now: datetime,
) -> LobbyLaunchReadiness:
    """Return the lock-stage readiness facts without exposing seat topology."""
    decision = evaluate_lobby_rows(
        members=members,
        seats=seats,
        expected_player_count=_runtime_ruleset_for_lobby(lobby).PLAYER_COUNT,
        # AI model builds are materialized only at launch.  The pre-launch
        # readiness contract therefore checks humans/roster topology here while
        # the frozen quote checks the selected AI roster separately.
        available_ai_build_count=None,
        now=now,
        stale_seconds=_stale_seconds(),
    )
    return LobbyLaunchReadiness(
        allowed=decision.allowed,
        reasons=[reason.value for reason in decision.reasons],
    )


__all__ = [
    "LaunchResponse",
    "LobbyAiRosterCard",
    "LobbyCreate",
    "LobbyLaunchReadiness",
    "LobbyRoster",
    "LobbySummary",
    "ReadyUpdate",
    "RosterMember",
    "router",
]
