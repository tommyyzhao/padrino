"""OpenSkill PlackettLuce-backed rating updates per game.

Wraps :mod:`openskill.models.PlackettLuce` so the runner can call a single
async entry point — :func:`update_ratings_for_game` — after each game
terminates and have every agent build's ``(GLOBAL, 'global')`` and
``(FACTION, 'TOWN' | 'MAFIA')`` rating row + audit event updated in place.

Per the v1 PRD this service does NOT update ``ROLE_FAMILY`` scope ratings.
A later story may add finer-grained scopes; until then the rating service
exposes exactly two scopes per game.

Conservative score is persisted as ``mu - 3 * sigma`` on every update so
leaderboard reads do not need to recompute it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from openskill.models import PlackettLuce
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction
from padrino.db.models import RatingEvent
from padrino.db.repositories import ratings as ratings_repo

INITIAL_MU: Final[float] = 25.0
INITIAL_SIGMA: Final[float] = 25.0 / 3.0

SCOPE_GLOBAL: Final[str] = "GLOBAL"
SCOPE_VALUE_GLOBAL: Final[str] = "global"
SCOPE_FACTION: Final[str] = "FACTION"


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


@dataclass(frozen=True, slots=True)
class GameResult:
    """Per-game outcome consumed by the rating service."""

    game_id: uuid.UUID
    winner: Literal["TOWN", "MAFIA", "DRAW"]
    seat_factions: Mapping[str, Faction]


def _ranks_for(winner: Literal["TOWN", "MAFIA", "DRAW"]) -> tuple[int, int]:
    """Return ``(town_rank, mafia_rank)`` — lower is better."""
    if winner == "TOWN":
        return (1, 2)
    if winner == "MAFIA":
        return (2, 1)
    return (1, 1)


async def _apply_scope_update(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    scope_type: str,
    town_scope_value: str,
    mafia_scope_value: str,
    town_seats: list[tuple[str, uuid.UUID]],
    mafia_seats: list[tuple[str, uuid.UUID]],
    winner: Literal["TOWN", "MAFIA", "DRAW"],
    model: PlackettLuce,
    now: datetime,
) -> list[RatingEvent]:
    """Run one PlackettLuce update for a single scope and persist rows + audit."""
    town_rows = []
    for _sid, ab_id in town_seats:
        row = await ratings_repo.get_or_create_rating(
            session,
            league_id=league_id,
            agent_build_id=ab_id,
            scope_type=scope_type,
            scope_value=town_scope_value,
            initial_mu=INITIAL_MU,
            initial_sigma=INITIAL_SIGMA,
            initial_conservative_score=_conservative(INITIAL_MU, INITIAL_SIGMA),
        )
        town_rows.append(row)

    mafia_rows = []
    for _sid, ab_id in mafia_seats:
        row = await ratings_repo.get_or_create_rating(
            session,
            league_id=league_id,
            agent_build_id=ab_id,
            scope_type=scope_type,
            scope_value=mafia_scope_value,
            initial_mu=INITIAL_MU,
            initial_sigma=INITIAL_SIGMA,
            initial_conservative_score=_conservative(INITIAL_MU, INITIAL_SIGMA),
        )
        mafia_rows.append(row)

    town_team = [model.create_rating([r.mu, r.sigma], name=str(r.id)) for r in town_rows]
    mafia_team = [model.create_rating([r.mu, r.sigma], name=str(r.id)) for r in mafia_rows]

    town_rank, mafia_rank = _ranks_for(winner)
    new_town_team, new_mafia_team = model.rate(
        [town_team, mafia_team], ranks=[town_rank, mafia_rank]
    )

    events: list[RatingEvent] = []
    for row, (sid, _ab_id), new in zip(town_rows, town_seats, new_town_team, strict=True):
        events.append(
            await _persist_one(
                session,
                row=row,
                new_mu=new.mu,
                new_sigma=new.sigma,
                league_id=league_id,
                game_id=game_id,
                scope_type=scope_type,
                scope_value=town_scope_value,
                public_player_id=sid,
                now=now,
            )
        )
    for row, (sid, _ab_id), new in zip(mafia_rows, mafia_seats, new_mafia_team, strict=True):
        events.append(
            await _persist_one(
                session,
                row=row,
                new_mu=new.mu,
                new_sigma=new.sigma,
                league_id=league_id,
                game_id=game_id,
                scope_type=scope_type,
                scope_value=mafia_scope_value,
                public_player_id=sid,
                now=now,
            )
        )
    return events


async def _persist_one(
    session: AsyncSession,
    *,
    row: object,
    new_mu: float,
    new_sigma: float,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    public_player_id: str | None = None,
    now: datetime,
) -> RatingEvent:
    """Update a single rating row + append the matching audit event."""
    from padrino.db.models import Rating

    assert isinstance(row, Rating)
    before_mu = float(row.mu)
    before_sigma = float(row.sigma)
    updated = await ratings_repo.update_rating(
        session,
        row.id,
        mu=new_mu,
        sigma=new_sigma,
        conservative_score=_conservative(new_mu, new_sigma),
        games=row.games + 1,
        last_game_at=now,
    )
    if updated is None:  # pragma: no cover — row was just inserted in this txn.
        msg = f"Rating row {row.id} disappeared between insert and update"
        raise RuntimeError(msg)
    return await ratings_repo.record_rating_event(
        session,
        league_id=league_id,
        game_id=game_id,
        agent_build_id=updated.agent_build_id,
        scope_type=scope_type,
        scope_value=scope_value,
        before_mu=before_mu,
        before_sigma=before_sigma,
        after_mu=new_mu,
        after_sigma=new_sigma,
        public_player_id=public_player_id,
    )


async def update_ratings_for_game(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_result: GameResult,
    agent_builds_by_seat: Mapping[str, uuid.UUID],
    now: datetime | None = None,
) -> list[RatingEvent]:
    """Apply OpenSkill updates for one game across GLOBAL + FACTION scopes.

    Updates two scopes per agent_build:

    * ``(scope_type='GLOBAL', scope_value='global')`` — town team vs. mafia
      team in a single Plackett-Luce update.
    * ``(scope_type='FACTION', scope_value in {'TOWN', 'MAFIA'})`` — the
      same head-to-head update, but each team's ratings live under their
      own faction-scope row so faction-specific skill accumulates
      independently.

    Returns the freshly-appended :class:`RatingEvent` audit rows in scope
    order (GLOBAL then FACTION) and seat order within each scope.
    """
    seats_sorted = sorted(game_result.seat_factions)
    town_seats: list[tuple[str, uuid.UUID]] = []
    mafia_seats: list[tuple[str, uuid.UUID]] = []
    for sid in seats_sorted:
        ab_id = agent_builds_by_seat[sid]
        if game_result.seat_factions[sid] is Faction.TOWN:
            town_seats.append((sid, ab_id))
        else:
            mafia_seats.append((sid, ab_id))

    _now = now if now is not None else datetime.now(UTC)
    model = PlackettLuce(mu=INITIAL_MU, sigma=INITIAL_SIGMA)
    events: list[RatingEvent] = []

    events.extend(
        await _apply_scope_update(
            session,
            league_id=league_id,
            game_id=game_result.game_id,
            scope_type=SCOPE_GLOBAL,
            town_scope_value=SCOPE_VALUE_GLOBAL,
            mafia_scope_value=SCOPE_VALUE_GLOBAL,
            town_seats=town_seats,
            mafia_seats=mafia_seats,
            winner=game_result.winner,
            model=model,
            now=_now,
        )
    )
    events.extend(
        await _apply_scope_update(
            session,
            league_id=league_id,
            game_id=game_result.game_id,
            scope_type=SCOPE_FACTION,
            town_scope_value=Faction.TOWN.value,
            mafia_scope_value=Faction.MAFIA.value,
            town_seats=town_seats,
            mafia_seats=mafia_seats,
            winner=game_result.winner,
            model=model,
            now=_now,
        )
    )

    return events


__all__ = [
    "INITIAL_MU",
    "INITIAL_SIGMA",
    "SCOPE_FACTION",
    "SCOPE_GLOBAL",
    "SCOPE_VALUE_GLOBAL",
    "GameResult",
    "update_ratings_for_game",
]
