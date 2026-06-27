"""Manual rollback for ranked Humans-Included human ratings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.anticheat.rating_rollback import (
    RatingRollbackGame,
    RatingRollbackPlayer,
    recompute_human_rating,
)
from padrino.core.enums import LeagueKind
from padrino.db.models import Game, GameSeat, HumanRating, HumanRatingEvent, League
from padrino.db.repositories import human_principals as human_principals_repo
from padrino.db.repositories import human_ratings as human_ratings_repo
from padrino.db.repositories import ranked_game_exclusions as exclusions_repo
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA, SCOPE_GLOBAL
from padrino.ratings.openskill_service import SCOPE_VALUE_GLOBAL as _SCOPE_VALUE_GLOBAL

SCOPE_VALUE_GLOBAL = _SCOPE_VALUE_GLOBAL

WinnerValue = Literal["TOWN", "MAFIA", "DRAW"]


class RankedRollbackError(ValueError):
    """HTTP-shaped rollback failure raised by the rating shell."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RankedRollbackResult:
    """Outcome of one manual ranked rating rollback."""

    principal_id: uuid.UUID
    human_player_id: str
    excluded_game_ids: tuple[uuid.UUID, ...]
    mu: float
    sigma: float
    conservative_score: float
    games: int
    audit_created: bool


async def rollback_ranked_human_rating(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    excluded_game_ids: list[uuid.UUID],
    reason: str,
    created_by: str,
    now: datetime,
) -> RankedRollbackResult:
    """Exclude games for one principal and recompute their GLOBAL human rating."""
    principal = await human_principals_repo.get_principal(session, principal_id)
    if principal is None or principal.deleted_at is not None:
        raise RankedRollbackError("principal_not_found", status_code=404)
    unique_excluded = tuple(sorted(set(excluded_game_ids)))
    if not unique_excluded:
        raise RankedRollbackError("excluded_game_ids_required", status_code=422)

    rating, events = await _resolve_rating_history(
        session,
        principal_id=principal_id,
        excluded_game_ids=set(unique_excluded),
    )
    rollback_games = await _rollback_games_for_events(session, events)
    result = recompute_human_rating(
        rollback_games,
        human_player_id=rating.human_player_id,
        excluded_game_ids={str(game_id) for game_id in unique_excluded},
        initial_mu=INITIAL_MU,
        initial_sigma=INITIAL_SIGMA,
    )
    included_event_times = [
        event.created_at for event in events if event.game_id not in set(unique_excluded)
    ]
    updated = await human_ratings_repo.update_human_rating(
        session,
        rating.id,
        mu=result.mu,
        sigma=result.sigma,
        conservative_score=result.conservative_score,
        games=result.games,
        updated_at=now,
        last_game_at=max(included_event_times, default=None),
    )
    if updated is None:  # pragma: no cover - loaded earlier in this transaction.
        raise RuntimeError(f"Human rating row {rating.id} disappeared during rollback")

    for game_id in unique_excluded:
        await exclusions_repo.record_exclusion(
            session,
            principal_id=principal_id,
            human_player_id=rating.human_player_id,
            game_id=game_id,
            reason=reason,
            created_by=created_by,
            created_at=now,
        )
    audit = await exclusions_repo.record_rollback_audit(
        session,
        principal_id=principal_id,
        human_player_id=rating.human_player_id,
        reason=reason,
        excluded_game_ids=[str(game_id) for game_id in unique_excluded],
        created_by=created_by,
        created_at=now,
    )
    return RankedRollbackResult(
        principal_id=principal_id,
        human_player_id=rating.human_player_id,
        excluded_game_ids=unique_excluded,
        mu=result.mu,
        sigma=result.sigma,
        conservative_score=result.conservative_score,
        games=result.games,
        audit_created=audit is not None,
    )


async def _resolve_rating_history(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    excluded_game_ids: set[uuid.UUID],
) -> tuple[HumanRating, list[HumanRatingEvent]]:
    stmt = (
        select(HumanRating)
        .join(League, League.id == HumanRating.league_id)
        .where(
            HumanRating.human_player_id == str(principal_id),
            HumanRating.scope_type == SCOPE_GLOBAL,
            HumanRating.scope_value == SCOPE_VALUE_GLOBAL,
            League.kind == LeagueKind.HUMANS_INCLUDED.value,
            League.ranked.is_(True),
        )
        .order_by(HumanRating.updated_at, HumanRating.id)
    )
    ratings = list((await session.execute(stmt)).scalars())
    if not ratings:
        raise RankedRollbackError("human_rating_not_found", status_code=404)

    for rating in ratings:
        events = await _events_for_rating(session, rating)
        event_game_ids = {event.game_id for event in events}
        if excluded_game_ids.issubset(event_game_ids):
            return rating, events
    raise RankedRollbackError("excluded_game_not_in_rating_history", status_code=422)


async def _events_for_rating(
    session: AsyncSession,
    rating: HumanRating,
) -> list[HumanRatingEvent]:
    stmt = (
        select(HumanRatingEvent)
        .where(
            HumanRatingEvent.league_id == rating.league_id,
            HumanRatingEvent.human_player_id == rating.human_player_id,
            HumanRatingEvent.scope_type == rating.scope_type,
            HumanRatingEvent.scope_value == rating.scope_value,
        )
        .order_by(
            HumanRatingEvent.created_at,
            HumanRatingEvent.game_id,
            HumanRatingEvent.id,
        )
    )
    return list((await session.execute(stmt)).scalars())


async def _rollback_games_for_events(
    session: AsyncSession,
    events: list[HumanRatingEvent],
) -> tuple[RatingRollbackGame, ...]:
    game_ids = [event.game_id for event in events]
    if not game_ids:
        return ()

    games = {
        game.id: game
        for game in (await session.execute(select(Game).where(Game.id.in_(game_ids)))).scalars()
    }
    seats_by_game: dict[uuid.UUID, list[GameSeat]] = {game_id: [] for game_id in game_ids}
    seat_stmt = (
        select(GameSeat)
        .where(GameSeat.game_id.in_(game_ids))
        .order_by(GameSeat.game_id, GameSeat.seat_index)
    )
    for seat in (await session.execute(seat_stmt)).scalars():
        seats_by_game.setdefault(seat.game_id, []).append(seat)

    event_stmt = select(HumanRatingEvent).where(
        HumanRatingEvent.game_id.in_(game_ids),
        HumanRatingEvent.scope_type == SCOPE_GLOBAL,
        HumanRatingEvent.scope_value == SCOPE_VALUE_GLOBAL,
    )
    human_events = {
        (event.game_id, event.human_player_id): event
        for event in (await session.execute(event_stmt)).scalars()
    }

    rollback_games: list[RatingRollbackGame] = []
    for event in events:
        game = games.get(event.game_id)
        if game is None:
            raise RankedRollbackError("game_not_found", status_code=422)
        winner = _winner_from_game(game)
        seats = seats_by_game.get(event.game_id, [])
        players = tuple(
            _player_for_seat(
                seat,
                target_event=event,
                human_events=human_events,
            )
            for seat in seats
        )
        rollback_games.append(
            RatingRollbackGame(
                game_id=str(event.game_id),
                winner=winner,
                players=players,
            )
        )
    return tuple(rollback_games)


def _player_for_seat(
    seat: GameSeat,
    *,
    target_event: HumanRatingEvent,
    human_events: dict[tuple[uuid.UUID, str], HumanRatingEvent],
) -> RatingRollbackPlayer:
    human_player_id: str | None
    if seat.public_player_id == target_event.public_player_id:
        human_player_id = target_event.human_player_id
    elif seat.occupant_principal_id is not None:
        human_player_id = str(seat.occupant_principal_id)
    else:
        human_player_id = None

    before_mu = INITIAL_MU
    before_sigma = INITIAL_SIGMA
    if human_player_id is not None:
        existing_event = human_events.get((seat.game_id, human_player_id))
        if existing_event is not None:
            before_mu = float(existing_event.before_mu)
            before_sigma = float(existing_event.before_sigma)
    faction = cast(Literal["TOWN", "MAFIA"], seat.faction)
    return RatingRollbackPlayer(
        public_player_id=seat.public_player_id,
        faction=faction,
        human_player_id=human_player_id,
        before_mu=before_mu,
        before_sigma=before_sigma,
    )


def _winner_from_game(game: Game) -> WinnerValue:
    result = game.terminal_result if isinstance(game.terminal_result, dict) else {}
    winner = result.get("winner")
    if winner in {"TOWN", "MAFIA", "DRAW"}:
        return cast(WinnerValue, winner)
    raise RankedRollbackError("game_terminal_result_missing_winner", status_code=422)


__all__ = [
    "RankedRollbackError",
    "RankedRollbackResult",
    "rollback_ranked_human_rating",
]
