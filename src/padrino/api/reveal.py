"""Shared API shell for the canonical endgame reveal projection.

The reveal schema is produced only by :mod:`padrino.core.reveal`. This module
does the impure work common to public and human-private routes: loading
``GameSeat`` rows, resolving exact model identity, and deriving terminal winner
state from either the finalized game row or the verified event log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.reveal import EndgameReveal, RevealModel, SeatRevealInput, project_endgame_reveal
from padrino.db.models import AgentBuild, Game, GameSeat, ModelConfig, ModelProvider
from padrino.db.repositories import events as events_repo
from padrino.runner.human_durability import replay_state_from_rows

GAME_NOT_FOUND_DETAIL = "game_not_found"
NOT_TERMINAL_DETAIL = "game_not_terminal"
WRONG_SEAT_DETAIL = "wrong_seat"


@dataclass(frozen=True, slots=True)
class TerminalRevealState:
    """Whether a game is terminal, plus the winner label if available."""

    is_terminal: bool
    winner: str | None


async def resolve_participant_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> GameSeat:
    """Return the seat occupied by ``principal_id`` in ``game_id``, or 403."""
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)
    return seat


def winner_from_terminal_result(terminal_result: dict[str, object] | None) -> str | None:
    """Extract the winner label from a finalized ``Game.terminal_result`` row."""
    if not isinstance(terminal_result, dict):
        return None
    raw_winner = terminal_result.get("winner")
    return str(raw_winner) if raw_winner is not None else None


async def resolve_terminal_reveal_state(
    session: AsyncSession,
    game: Game,
) -> TerminalRevealState:
    """Resolve terminal status for a reveal route.

    Normal runner-finalized games carry ``Game.terminal_result``. Private human
    tests and restart edge cases can have a terminal event log before a row-level
    terminal payload is materialized, so replay the verified chain as a fallback.
    """
    if game.terminal_result is not None:
        return TerminalRevealState(
            is_terminal=True,
            winner=winner_from_terminal_result(game.terminal_result),
        )

    rows = await events_repo.list_events(session, game.id)
    if not rows:
        return TerminalRevealState(is_terminal=False, winner=None)

    state, _event_log = replay_state_from_rows(rows)
    if state.terminal_result is None:
        return TerminalRevealState(is_terminal=False, winner=None)
    return TerminalRevealState(is_terminal=True, winner=str(state.terminal_result))


async def _resolve_seat_models(
    session: AsyncSession,
    seats: list[GameSeat],
) -> dict[uuid.UUID, RevealModel]:
    """Resolve the exact model identity for every AI-occupied seat."""
    build_ids: set[uuid.UUID] = set()
    for seat in seats:
        build_id = seat.takeover_agent_build_id or seat.agent_build_id
        if build_id is not None:
            build_ids.add(build_id)
    if not build_ids:
        return {}

    stmt = (
        select(AgentBuild, ModelConfig, ModelProvider)
        .join(ModelConfig, AgentBuild.model_config_id == ModelConfig.id)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id)
        .where(AgentBuild.id.in_(build_ids))
    )
    out: dict[uuid.UUID, RevealModel] = {}
    for build, mc, provider in (await session.execute(stmt)).all():
        out[build.id] = RevealModel(
            provider=provider.name,
            model_name=mc.model_name,
            model_version=mc.model_version,
            agent_build_id=str(build.id),
            display_name=build.display_name,
        )
    return out


async def build_endgame_reveal(
    session: AsyncSession,
    game: Game,
    *,
    winner: str | None = None,
) -> EndgameReveal:
    """Build the canonical endgame reveal for ``game`` from DB-resolved inputs."""
    seats_stmt = select(GameSeat).where(GameSeat.game_id == game.id)
    seats = list((await session.execute(seats_stmt)).scalars())
    models = await _resolve_seat_models(session, seats)

    inputs: list[SeatRevealInput] = []
    for seat in seats:
        build_id = seat.takeover_agent_build_id or seat.agent_build_id
        inputs.append(
            SeatRevealInput(
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                seat_kind=seat.seat_kind,
                role=seat.role,
                faction=seat.faction,
                alive=seat.alive,
                taken_over_at_phase=seat.taken_over_at_phase,
                model=models.get(build_id) if build_id is not None else None,
            )
        )

    return project_endgame_reveal(
        game_id=str(game.id),
        ruleset_id=game.ruleset_id,
        winner=winner if winner is not None else winner_from_terminal_result(game.terminal_result),
        seats=inputs,
    )


async def build_participant_reveal(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> EndgameReveal:
    """Return a terminal private reveal for a participant in ``game_id``.

    Unknown games are 404, non-participants are 403, and pre-terminal games are
    409. Broadcast state is intentionally ignored here: private human games are
    participant-visible after terminal even when never public-broadcastable.
    """
    game = await session.get(Game, game_id)
    if game is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    await resolve_participant_seat(session, game_id=game_id, principal_id=principal_id)

    terminal = await resolve_terminal_reveal_state(session, game)
    if not terminal.is_terminal:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_TERMINAL_DETAIL)

    return await build_endgame_reveal(session, game, winner=terminal.winner)


__all__ = [
    "GAME_NOT_FOUND_DETAIL",
    "NOT_TERMINAL_DETAIL",
    "WRONG_SEAT_DETAIL",
    "TerminalRevealState",
    "build_endgame_reveal",
    "build_participant_reveal",
    "resolve_participant_seat",
    "resolve_terminal_reveal_state",
    "winner_from_terminal_result",
]
