"""Game inspection routes (US-044).

GET /games/{id} returns a public summary, GET /games/{id}/events streams
events filtered by visibility (admin-token gated for non-public), GET
/games/{id}/transcript returns the post-game artifact once the game has
terminated, and POST /games/{id}/replay re-runs the hash-chain through
:func:`padrino.core.engine.replay.replay_event_log` and reports whether
the stored hashes survived the replay.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_admin_token, get_session
from padrino.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    CursorPage,
    paginate_keyset,
)
from padrino.core.engine.event_log import StoredEvent
from padrino.core.engine.replay import ReplayHashMismatchError, replay_event_log
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo

router = APIRouter()

VisibilityFilter = Literal["public", "all"]


class GameDetailResponse(BaseModel):
    id: uuid.UUID
    status: str
    terminal_result: dict[str, Any] | None
    current_phase: str | None
    seat_count: int


class EventEntry(BaseModel):
    sequence: int
    event_type: str
    phase: str
    visibility: str
    actor_player_id: str | None
    payload: dict[str, Any]
    prev_event_hash: str
    event_hash: str


class EventsResponse(BaseModel):
    game_id: uuid.UUID
    visibility: VisibilityFilter
    events: list[EventEntry]


class ChatEntry(BaseModel):
    sequence: int
    phase: str
    actor_player_id: str
    text: str
    round_index: int | None = None


class RoleReveal(BaseModel):
    public_player_id: str
    role: str
    faction: str


class ActionEntry(BaseModel):
    sequence: int
    phase: str
    event_type: str
    actor_player_id: str | None
    payload: dict[str, Any]


class Outcome(BaseModel):
    winner: str
    reason: str


class TranscriptResponse(BaseModel):
    game_id: uuid.UUID
    public_chat: list[ChatEntry]
    mafia_chat: list[ChatEntry]
    roles: list[RoleReveal]
    actions: list[ActionEntry]
    outcome: Outcome


class ReplayResponse(BaseModel):
    game_id: uuid.UUID
    replay_status: Literal["PASS", "FAIL"]
    final_event_hash: str


_PUBLIC = "PUBLIC"
_ACTION_EVENT_TYPES = (
    "VoteSubmitted",
    "MafiaKillVoteSubmitted",
    "ProtectSubmitted",
    "InvestigateSubmitted",
)


async def _seat_count(session: AsyncSession, game_id: uuid.UUID) -> int:
    stmt = select(GameSeat).where(GameSeat.game_id == game_id)
    result = await session.execute(stmt)
    return len(list(result.scalars()))


async def _game_or_404(session: AsyncSession, game_id: uuid.UUID) -> Game:
    obj = await games_repo.get(session, game_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"game {game_id} not found",
        )
    return obj


class GameListEntry(BaseModel):
    id: uuid.UUID
    status: str
    ruleset_id: str
    gauntlet_id: uuid.UUID | None
    terminal_result: dict[str, Any] | None
    current_phase: str | None


class GameListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    status: str | None = None
    gauntlet_id: uuid.UUID | None = None
    ruleset_id: str | None = None


@router.get("/games", response_model=CursorPage[GameListEntry])
async def list_games(
    query: Annotated[GameListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[GameListEntry]:
    stmt = select(Game)
    if query.status is not None:
        stmt = stmt.where(Game.status == query.status)
    if query.gauntlet_id is not None:
        stmt = stmt.where(Game.gauntlet_id == query.gauntlet_id)
    if query.ruleset_id is not None:
        stmt = stmt.where(Game.ruleset_id == query.ruleset_id)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=Game.created_at,
        id_col=Game.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [
        GameListEntry(
            id=g.id,
            status=g.status,
            ruleset_id=g.ruleset_id,
            gauntlet_id=g.gauntlet_id,
            terminal_result=g.terminal_result,
            current_phase=g.current_phase,
        )
        for g in rows
    ]
    return CursorPage[GameListEntry](items=items, next_cursor=next_cursor)


@router.get("/games/{game_id}", response_model=GameDetailResponse)
async def get_game(
    game_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> GameDetailResponse:
    obj = await _game_or_404(session, game_id)
    seats = await _seat_count(session, game_id)
    return GameDetailResponse(
        id=obj.id,
        status=obj.status,
        terminal_result=obj.terminal_result,
        current_phase=obj.current_phase,
        seat_count=seats,
    )


@router.get("/games/{game_id}/events", response_model=EventsResponse)
async def list_game_events(
    game_id: uuid.UUID,
    visibility: VisibilityFilter = Query(default="public"),
    x_padrino_admin_token: str | None = Header(default=None, alias="X-Padrino-Admin-Token"),
    session: AsyncSession = Depends(get_session),
    admin_token: str | None = Depends(get_admin_token),
) -> EventsResponse:
    await _game_or_404(session, game_id)

    if visibility == "all":
        if admin_token is None or x_padrino_admin_token != admin_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin token required to read non-public events",
            )
        rows = await events_repo.list_events(session, game_id)
    else:
        rows = await events_repo.list_events(session, game_id, visibility_filter=_PUBLIC)

    entries = [
        EventEntry(
            sequence=r.sequence,
            event_type=r.event_type,
            phase=r.phase,
            visibility=r.visibility,
            actor_player_id=r.actor_player_id,
            payload=r.payload,
            prev_event_hash=r.prev_event_hash,
            event_hash=r.event_hash,
        )
        for r in rows
    ]
    return EventsResponse(game_id=game_id, visibility=visibility, events=entries)


@router.get("/games/{game_id}/transcript", response_model=TranscriptResponse)
async def get_game_transcript(
    game_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> TranscriptResponse:
    game = await _game_or_404(session, game_id)
    if game.status != "COMPLETED" or game.terminal_result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"game {game_id} has not terminated yet",
        )

    rows = await events_repo.list_events(session, game_id)

    public_chat: list[ChatEntry] = []
    mafia_chat: list[ChatEntry] = []
    actions: list[ActionEntry] = []
    role_assignments: list[RoleReveal] = []

    for r in rows:
        if r.event_type == "RolesAssigned":
            assignments = r.payload.get("assignments", [])
            for a in assignments:
                role_assignments.append(
                    RoleReveal(
                        public_player_id=a["public_player_id"],
                        role=a["role"],
                        faction=a["faction"],
                    )
                )
        elif r.event_type == "PublicMessageSubmitted" and r.actor_player_id is not None:
            public_chat.append(
                ChatEntry(
                    sequence=r.sequence,
                    phase=r.phase,
                    actor_player_id=r.actor_player_id,
                    text=str(r.payload.get("text", "")),
                    round_index=r.payload.get("round_index"),
                )
            )
        elif r.event_type == "PrivateMessageSubmitted" and r.actor_player_id is not None:
            mafia_chat.append(
                ChatEntry(
                    sequence=r.sequence,
                    phase=r.phase,
                    actor_player_id=r.actor_player_id,
                    text=str(r.payload.get("text", "")),
                )
            )
        elif r.event_type in _ACTION_EVENT_TYPES:
            actions.append(
                ActionEntry(
                    sequence=r.sequence,
                    phase=r.phase,
                    event_type=r.event_type,
                    actor_player_id=r.actor_player_id,
                    payload=r.payload,
                )
            )

    outcome = Outcome(
        winner=str(game.terminal_result["winner"]),
        reason=str(game.terminal_result["reason"]),
    )

    return TranscriptResponse(
        game_id=game_id,
        public_chat=public_chat,
        mafia_chat=mafia_chat,
        roles=role_assignments,
        actions=actions,
        outcome=outcome,
    )


@router.post("/games/{game_id}/replay", response_model=ReplayResponse)
async def replay_game(
    game_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ReplayResponse:
    await _game_or_404(session, game_id)
    rows = await events_repo.list_events(session, game_id)
    stored: list[StoredEvent] = []
    for r in rows:
        body: dict[str, Any] = {
            "event_type": r.event_type,
            "sequence": r.sequence,
            "phase": r.phase,
            "visibility": r.visibility,
            "actor_player_id": r.actor_player_id,
            "payload": dict(r.payload),
        }
        stored.append(
            StoredEvent(
                sequence=r.sequence,
                prev_event_hash=r.prev_event_hash,
                event_hash=r.event_hash,
                body=body,
            )
        )

    try:
        log = replay_event_log(stored)
    except ReplayHashMismatchError as exc:
        return ReplayResponse(
            game_id=game_id,
            replay_status="FAIL",
            final_event_hash=exc.actual,
        )

    return ReplayResponse(
        game_id=game_id,
        replay_status="PASS",
        final_event_hash=log.head_hash,
    )


__all__ = [
    "ActionEntry",
    "ChatEntry",
    "EventEntry",
    "EventsResponse",
    "GameDetailResponse",
    "Outcome",
    "ReplayResponse",
    "RoleReveal",
    "TranscriptResponse",
    "router",
]
