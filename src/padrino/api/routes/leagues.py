"""League creation route (US-043).

POST /leagues creates a league row. Leagues group gauntlets that share a
ruleset and ranking mode, and provide the per-league cohort used by the
leaderboard.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.repositories import leagues as leagues_repo

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


__all__ = ["LeagueCreate", "LeagueResponse", "router"]
