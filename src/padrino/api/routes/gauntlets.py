"""Gauntlet creation and inspection routes (US-043).

POST /gauntlets validates the roster and triggers scheduling — the body of
the route mirrors :mod:`padrino.gauntlets.scheduler` but runs inside the
``get_session``-managed transaction so validation reads do not race against
inserts.

GET /gauntlets/{id} returns the gauntlet row, its child games, and the
aggregate diagnostics from :mod:`padrino.gauntlets.completion` so an operator
can poll progress without waiting for the gauntlet to finalize.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.models import GameEvent
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.gauntlets.completion import diagnostics_for_games
from padrino.gauntlets.scheduler import (
    MAX_CLONE_COUNT,
    MIN_CLONE_COUNT,
    derive_game_seed,
)

router = APIRouter()

_GAUNTLET_SEED_BYTES = 32  # 256-bit


def _generate_gauntlet_seed() -> str:
    return os.urandom(_GAUNTLET_SEED_BYTES).hex()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GauntletCreate(_StrictModel):
    league_id: uuid.UUID
    ruleset_id: str = Field(min_length=1)
    prompt_version_id: uuid.UUID
    clone_count: int
    gauntlet_seed: str | None = None
    roster: Annotated[list[uuid.UUID], Field(min_length=1)]


class GauntletCreateResponse(BaseModel):
    gauntlet_id: uuid.UUID
    status: str
    game_ids: list[uuid.UUID]


class GameSummary(BaseModel):
    id: uuid.UUID
    status: str
    terminal_result: str | None
    terminal_reason: str | None
    current_phase: str | None


class DiagnosticsSummary(BaseModel):
    games_completed: int
    timeout_rate: float
    invalid_action_rate: float
    average_public_message_chars: float


class GauntletDetailResponse(BaseModel):
    id: uuid.UUID
    league_id: uuid.UUID
    ruleset_id: str
    prompt_version_id: uuid.UUID
    clone_count: int
    gauntlet_seed: str
    ranked: bool
    status: str
    created_at: datetime
    completed_at: datetime | None
    games: list[GameSummary]
    diagnostics: DiagnosticsSummary


@router.post(
    "/gauntlets",
    response_model=GauntletCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_gauntlet_route(
    body: GauntletCreate,
    session: AsyncSession = Depends(get_session),
) -> GauntletCreateResponse:
    if len(body.roster) != 7:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"roster must have exactly 7 entries, got {len(body.roster)}",
        )
    if not (MIN_CLONE_COUNT <= body.clone_count <= MAX_CLONE_COUNT):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"clone_count must be in [{MIN_CLONE_COUNT}, {MAX_CLONE_COUNT}], "
                f"got {body.clone_count}"
            ),
        )

    league = await leagues_repo.get(session, body.league_id)
    if league is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown league_id: {body.league_id}",
        )

    pv = await prompt_versions_repo.get(session, body.prompt_version_id)
    if pv is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown prompt_version_id: {body.prompt_version_id}",
        )
    if pv.ruleset_id != body.ruleset_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"prompt_version ruleset {pv.ruleset_id!r} does not match "
                f"gauntlet ruleset {body.ruleset_id!r}"
            ),
        )

    for ab_id in body.roster:
        ab = await agent_builds_repo.get(session, ab_id)
        if ab is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown agent_build_id: {ab_id}",
            )

    seed = body.gauntlet_seed if body.gauntlet_seed is not None else _generate_gauntlet_seed()

    gauntlet = await gauntlets_repo.create(
        session,
        league_id=body.league_id,
        ruleset_id=body.ruleset_id,
        prompt_version_id=body.prompt_version_id,
        clone_count=body.clone_count,
        gauntlet_seed=seed,
        ranked=league.ranked,
        status="QUEUED",
    )
    for slot_index, agent_build_id in enumerate(body.roster):
        await gauntlets_repo.add_roster_slot(session, gauntlet.id, slot_index, agent_build_id)

    game_ids: list[uuid.UUID] = []
    for i in range(body.clone_count):
        game = await games_repo.create(
            session,
            ruleset_id=body.ruleset_id,
            game_seed=derive_game_seed(seed, i),
            gauntlet_id=gauntlet.id,
        )
        game_ids.append(game.id)

    return GauntletCreateResponse(
        gauntlet_id=gauntlet.id,
        status=gauntlet.status,
        game_ids=game_ids,
    )


@router.get("/gauntlets/{gauntlet_id}", response_model=GauntletDetailResponse)
async def get_gauntlet(
    gauntlet_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> GauntletDetailResponse:
    obj = await gauntlets_repo.get(session, gauntlet_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"gauntlet {gauntlet_id} not found",
        )
    games = await games_repo.list_by_gauntlet(session, gauntlet_id)
    game_ids = [g.id for g in games]
    if game_ids:
        terminal_stmt = (
            select(GameEvent.game_id)
            .where(
                GameEvent.game_id.in_(game_ids),
                GameEvent.event_type == "GameTerminated",
            )
            .distinct()
        )
        terminal_ids = list((await session.execute(terminal_stmt)).scalars().all())
    else:
        terminal_ids = []
    diagnostics = await diagnostics_for_games(session, terminal_ids)
    return GauntletDetailResponse(
        id=obj.id,
        league_id=obj.league_id,
        ruleset_id=obj.ruleset_id,
        prompt_version_id=obj.prompt_version_id,
        clone_count=obj.clone_count,
        gauntlet_seed=obj.gauntlet_seed,
        ranked=obj.ranked,
        status=obj.status,
        created_at=obj.created_at,
        completed_at=obj.completed_at,
        games=[
            GameSummary(
                id=g.id,
                status=g.status,
                terminal_result=g.terminal_result,
                terminal_reason=g.terminal_reason,
                current_phase=g.current_phase,
            )
            for g in games
        ],
        diagnostics=DiagnosticsSummary(
            games_completed=diagnostics.games_completed,
            timeout_rate=diagnostics.timeout_rate,
            invalid_action_rate=diagnostics.invalid_action_rate,
            average_public_message_chars=diagnostics.average_public_message_chars,
        ),
    )


__all__ = [
    "DiagnosticsSummary",
    "GameSummary",
    "GauntletCreate",
    "GauntletCreateResponse",
    "GauntletDetailResponse",
    "router",
]
