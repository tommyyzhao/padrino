"""League creation + leaderboard routes (US-043, US-045).

``POST /leagues`` creates a league row. Leagues group gauntlets that share a
ruleset and ranking mode, and provide the per-league cohort used by the
leaderboard.

``GET /leagues/{id}/leaderboard`` returns the per-agent_build leaderboard
contract from ``prd.md §10.4`` — sorted by conservative_score desc with a
provisional flag computed from the same thresholds the gauntlet completion
service uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.repositories import leagues as leagues_repo
from padrino.leaderboards.service import compute_leaderboard, entry_to_response

router = APIRouter()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeagueCreate(_StrictModel):
    name: str = Field(min_length=1)
    ruleset_id: str = Field(min_length=1)
    ranked: bool


class LeagueResponse(BaseModel):
    id: uuid.UUID
    name: str
    ruleset_id: str
    ranked: bool
    created_at: datetime


@router.post(
    "/leagues",
    response_model=LeagueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_league(
    body: LeagueCreate,
    session: AsyncSession = Depends(get_session),
) -> LeagueResponse:
    obj = await leagues_repo.create(
        session,
        name=body.name,
        ruleset_id=body.ruleset_id,
        ranked=body.ranked,
    )
    return LeagueResponse(
        id=obj.id,
        name=obj.name,
        ruleset_id=obj.ruleset_id,
        ranked=obj.ranked,
        created_at=obj.created_at,
    )


class LeaderboardEntryResponse(BaseModel):
    agent_build_id: uuid.UUID
    display_name: str
    games: int
    wins: int
    draws: int
    losses: int
    mu: float
    sigma: float
    conservative_score: float
    timeout_rate: float
    invalid_action_rate: float
    public_message_avg_chars: float
    role_family_breakdown: dict[str, dict[str, float]]
    provisional: bool


class LeaderboardResponse(BaseModel):
    leaderboard_id: str
    ruleset_id: str
    prompt_version: str
    rating_model: str
    entries: list[LeaderboardEntryResponse]


@router.get("/leagues/{league_id}/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    league_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> LeaderboardResponse:
    league = await leagues_repo.get(session, league_id)
    if league is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"league {league_id} not found",
        )
    leaderboard = await compute_leaderboard(
        session, league_id=league_id, ruleset_id=league.ruleset_id
    )
    entries: list[dict[str, Any]] = [entry_to_response(e) for e in leaderboard.entries]
    return LeaderboardResponse(
        leaderboard_id=leaderboard.leaderboard_id,
        ruleset_id=leaderboard.ruleset_id,
        prompt_version=leaderboard.prompt_version,
        rating_model=leaderboard.rating_model,
        entries=[LeaderboardEntryResponse(**entry) for entry in entries],
    )


__all__ = [
    "LeaderboardEntryResponse",
    "LeaderboardResponse",
    "LeagueCreate",
    "LeagueResponse",
    "router",
]
