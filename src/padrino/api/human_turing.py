"""Authenticated post-terminal spot-the-AI guess channel (US-144).

After a human game terminates, each human submits ONE guess assigning HUMAN/AI to
every OTHER seat over the existing human channel. This is a thin post-terminal
step, NOT a heavyweight new FSM phase: the impure shell

* resolves the caller's seat from ``occupant_principal_id`` (a human may only
  guess for the seat they occupy - a wrong-seat submission is rejected);
* replays the hash-chained event log and requires the game to be TERMINAL (a
  guess before the game ends is rejected - the imitation-game reveal is a
  post-game step);
* builds the seat truth from the ``GameSeat`` rows (a seat finished as a human iff
  its ``seat_kind == HUMAN``; an ``AI_TAKEOVER`` seat finished as AI, matching the
  canonical reveal) and hands it with the submitted guess to the pure
  :func:`padrino.core.turing.scoring.score_guess`;
* persists the guess + the guesser's personal detection accuracy.

Exactly one guess per ``(game_id, guesser)``: a re-submission returns the stored
guess + score rather than re-scoring (a guesser guesses once). There is NO
leaderboard (decision 6) - the result is the guesser's personal stat only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.human_seat_auth import resolve_human_game_seat
from padrino.core.enums import SeatKind
from padrino.core.turing import score_guess
from padrino.db.models import GameSeat
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_turing_guesses as guesses_repo
from padrino.runner.human_durability import replay_state_from_rows

GAME_NOT_FOUND_DETAIL = "game_not_found"
WRONG_SEAT_DETAIL = "wrong_seat"
NOT_TERMINAL_DETAIL = "game_not_terminal"
INVALID_GUESS_DETAIL = "invalid_guess"


@dataclass(frozen=True, slots=True)
class GuessOutcome:
    """The outcome of accepting (or replaying) one spot-the-AI guess."""

    guesser_public_id: str
    total: int
    correct: int
    accuracy: str
    idempotent_replay: bool


def _seat_truth(seats: list[GameSeat]) -> dict[str, bool]:
    """Map each seat's ``public_player_id`` to whether a human FINISHED it.

    A seat finished as a human iff ``seat_kind == HUMAN``; an ``AI`` or
    ``AI_TAKEOVER`` seat finished as AI (matching the canonical reveal, which
    fails closed to AI for any unknown kind).
    """
    return {seat.public_player_id: seat.seat_kind == SeatKind.HUMAN.value for seat in seats}


async def submit_guess(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    guess: dict[str, str],
    now: datetime,
) -> GuessOutcome:
    """Score and persist a human's post-terminal spot-the-AI guess.

    Raises :class:`fastapi.HTTPException` for an unknown game (404), a wrong-seat
    submission (403), a not-yet-terminal game (409), or an invalid guess (422 -
    an unknown seat, an illegal label, or a guess that does not cover every other
    seat). A re-submission returns the stored guess + score (a guesser guesses
    once).
    """
    seat_row = await resolve_human_game_seat(
        session,
        game_id=game_id,
        principal_id=principal_id,
        wrong_seat_detail=WRONG_SEAT_DETAIL,
    )

    existing = await guesses_repo.get_for_guesser(
        session, game_id=game_id, guesser_public_id=seat_row.public_player_id
    )
    if existing is not None:
        return GuessOutcome(
            guesser_public_id=existing.guesser_public_id,
            total=existing.total,
            correct=existing.correct,
            accuracy=existing.accuracy,
            idempotent_replay=True,
        )

    rows = await events_repo.list_events(session, game_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    state, _event_log = replay_state_from_rows(rows)
    if state.terminal_result is None:
        # The imitation-game guess is a post-game step; a live game has no reveal.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_TERMINAL_DETAIL)

    seats_stmt = select(GameSeat).where(GameSeat.game_id == game_id)
    seats = list((await session.execute(seats_stmt)).scalars())
    truth = _seat_truth(seats)

    try:
        score = score_guess(
            guesser_public_id=seat_row.public_player_id,
            guess=guess,
            truth=truth,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=INVALID_GUESS_DETAIL,
        ) from exc

    record = await guesses_repo.record(
        session,
        game_id=game_id,
        guesser_public_id=seat_row.public_player_id,
        guess=dict(guess),
        total=score.total,
        correct=score.correct,
        accuracy=score.accuracy,
        created_at=now,
    )
    return GuessOutcome(
        guesser_public_id=record.guesser_public_id,
        total=record.total,
        correct=record.correct,
        accuracy=record.accuracy,
        idempotent_replay=False,
    )


async def get_own_result(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> GuessOutcome | None:
    """Return the caller's own guess result, or None if they have not guessed.

    Gates the reveal endpoint's personal accuracy: a viewer sees their accuracy
    ONLY after submitting their guess (a 403 if they do not occupy a seat).
    """
    seat_row = await resolve_human_game_seat(
        session,
        game_id=game_id,
        principal_id=principal_id,
        wrong_seat_detail=WRONG_SEAT_DETAIL,
    )
    existing = await guesses_repo.get_for_guesser(
        session, game_id=game_id, guesser_public_id=seat_row.public_player_id
    )
    if existing is None:
        return None
    return GuessOutcome(
        guesser_public_id=existing.guesser_public_id,
        total=existing.total,
        correct=existing.correct,
        accuracy=existing.accuracy,
        idempotent_replay=True,
    )


__all__ = [
    "GAME_NOT_FOUND_DETAIL",
    "INVALID_GUESS_DETAIL",
    "NOT_TERMINAL_DETAIL",
    "WRONG_SEAT_DETAIL",
    "GuessOutcome",
    "get_own_result",
    "submit_guess",
]
