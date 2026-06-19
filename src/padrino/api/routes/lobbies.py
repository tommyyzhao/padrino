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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.api.human_auth import HumanPrincipalContext, require_human
from padrino.core.composition import CompositionSummary, composition_summary
from padrino.core.enums import IdentityMode, LobbySeatKind, LobbyStakes
from padrino.core.rulesets import get_ruleset
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import lobbies as lobbies_repo

router = APIRouter()


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
    host_principal_id: uuid.UUID
    league_id: uuid.UUID
    game_id: uuid.UUID | None
    member_count: int
    composition: CompositionSummary


@router.post("/lobbies", response_model=LobbySummary, status_code=status.HTTP_201_CREATED)
async def create_lobby(
    body: LobbyCreate,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> LobbySummary:
    """Create a private friend lobby owned by the calling human.

    The lobby is OPEN and CASUAL; the host occupies seat 0 (HUMAN). The remaining
    seats are AI: the host's pre-picked human-eligible models fill them in order,
    any beyond the pre-pick are left empty for curated auto-fill (US-149).
    """
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
    — never a per-seat human/AI map.
    """
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    if lobby is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lobby_not_found")
    member = await lobbies_repo.get_member(
        session, lobby_id=lobby_id, principal_id=ctx.principal_id
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lobby_not_found")
    return await _summary(session, lobby_id=lobby_id)


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
        host_principal_id=lobby.host_principal_id,
        league_id=lobby.league_id,
        game_id=lobby.game_id,
        member_count=len(members),
        composition=composition,
    )


__all__ = ["LobbyCreate", "LobbySummary", "router"]
