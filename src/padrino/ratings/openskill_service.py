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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from openskill.models import PlackettLuce
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction, RatingContextKind
from padrino.core.rulesets.canonicality import canonical_team_ranks_for_outcome
from padrino.db.models import (
    Game,
    GameSeat,
    PlacementRating,
    PlacementRatingEvent,
    RatingContext,
    RatingEvent,
)
from padrino.db.repositories import placement_ratings as placement_ratings_repo
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.db.repositories import ratings as ratings_repo

INITIAL_MU: Final[float] = 25.0
INITIAL_SIGMA: Final[float] = 25.0 / 3.0

SCOPE_GLOBAL: Final[str] = "GLOBAL"
SCOPE_VALUE_GLOBAL: Final[str] = "global"
SCOPE_FACTION: Final[str] = "FACTION"
PLACEMENT_DRAW: Final[str] = "DRAW"


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


@dataclass(frozen=True, slots=True)
class GameResult:
    """Per-game outcome consumed by the rating service."""

    game_id: uuid.UUID
    winner: Literal["TOWN", "MAFIA", "DRAW"]
    seat_factions: Mapping[str, Faction]


@dataclass(frozen=True, slots=True)
class PairedGameResult:
    """One leg of a mirror-paired rating update."""

    game_id: uuid.UUID
    winner: Literal["TOWN", "MAFIA", "DRAW"]
    seat_factions: Mapping[str, Faction]
    agent_builds_by_seat: Mapping[str, uuid.UUID]


@dataclass(frozen=True, slots=True)
class PlacementGameResult:
    """Per-game multi-outcome placement result for non-canonical contexts."""

    game_id: uuid.UUID
    winner: str
    seat_groups: Mapping[str, str]


def _ranks_for(winner: Literal["TOWN", "MAFIA", "DRAW"]) -> tuple[int, int]:
    """Return ``(town_rank, mafia_rank)`` — lower is better."""
    ranks = canonical_team_ranks_for_outcome(winner)
    return (ranks["TOWN"], ranks["MAFIA"])


async def _apply_scope_update(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    ruleset_id: str,
    rating_context_id: uuid.UUID,
    game_seed: str,
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
            ruleset_id=ruleset_id,
            rating_context_id=rating_context_id,
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
            ruleset_id=ruleset_id,
            rating_context_id=rating_context_id,
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
                new_mu=row.mu if winner == "DRAW" else new.mu,
                new_sigma=new.sigma,
                league_id=league_id,
                game_id=game_id,
                ruleset_id=ruleset_id,
                rating_context_id=rating_context_id,
                game_seed=game_seed,
                team_outcome=winner,
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
                new_mu=row.mu if winner == "DRAW" else new.mu,
                new_sigma=new.sigma,
                league_id=league_id,
                game_id=game_id,
                ruleset_id=ruleset_id,
                rating_context_id=rating_context_id,
                game_seed=game_seed,
                team_outcome=winner,
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
    ruleset_id: str,
    rating_context_id: uuid.UUID,
    game_seed: str,
    team_outcome: str,
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
        ruleset_id=ruleset_id,
        rating_context_id=rating_context_id,
        game_seed=game_seed,
        team_outcome=team_outcome,
        scope_type=scope_type,
        scope_value=scope_value,
        before_mu=before_mu,
        before_sigma=before_sigma,
        after_mu=new_mu,
        after_sigma=new_sigma,
        public_player_id=public_player_id,
    )


async def _persist_one_placement(
    session: AsyncSession,
    *,
    row: PlacementRating,
    new_mu: float,
    new_sigma: float,
    game_id: uuid.UUID,
    rating_context_id: uuid.UUID,
    game_seed: str,
    team_outcome: str,
    scope_type: str,
    scope_value: str,
    public_player_id: str | None = None,
    now: datetime,
) -> PlacementRatingEvent:
    """Update one placement row + append the matching sibling audit event."""
    before_mu = float(row.mu)
    before_sigma = float(row.sigma)
    updated = await placement_ratings_repo.update_placement_rating(
        session,
        row.id,
        mu=new_mu,
        sigma=new_sigma,
        conservative_score=_conservative(new_mu, new_sigma),
        games=row.games + 1,
        last_game_at=now,
    )
    if updated is None:  # pragma: no cover — row was just inserted in this txn.
        msg = f"Placement rating row {row.id} disappeared between insert and update"
        raise RuntimeError(msg)
    return await placement_ratings_repo.record_placement_rating_event(
        session,
        rating_context_id=rating_context_id,
        game_id=game_id,
        game_seed=game_seed,
        team_outcome=team_outcome,
        agent_build_id=updated.agent_build_id,
        scope_type=scope_type,
        scope_value=scope_value,
        before_mu=before_mu,
        before_sigma=before_sigma,
        after_mu=new_mu,
        after_sigma=new_sigma,
        public_player_id=public_player_id,
    )


async def _resolve_canonical_metadata(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
) -> tuple[Game, RatingContext] | None:
    game = await session.get(Game, game_id)
    if game is None:
        return None
    context = await rating_contexts_repo.resolve_canonical_team_context(
        session,
        league_id=league_id,
        ruleset_id=game.ruleset_id,
    )
    if context is None:
        return None
    return game, context


async def _resolve_placement_metadata(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> tuple[Game, RatingContext] | None:
    """Resolve a non-canonical PLACEMENT context for a game, fail-closed."""
    game = await session.get(Game, game_id)
    if game is None:
        return None

    declared = rating_contexts_repo.declared_for_ruleset(game.ruleset_id)
    if declared is not None and (
        declared.kind is not RatingContextKind.PLACEMENT or declared.is_canonical
    ):
        return None

    context = await rating_contexts_repo.get_by_ruleset_kind(
        session,
        ruleset_id=game.ruleset_id,
        kind=RatingContextKind.PLACEMENT,
    )
    if context is None:
        return None
    if context.kind != RatingContextKind.PLACEMENT.value or context.is_canonical:
        return None
    return game, context


def _placement_groups_for(result: PlacementGameResult) -> list[str]:
    """Return deterministic placement group order after validating the result."""
    groups = sorted(set(result.seat_groups.values()))
    if len(groups) < 2:
        raise ValueError("placement rating requires at least two outcome groups")
    if result.winner != PLACEMENT_DRAW and result.winner not in groups:
        raise ValueError(f"placement winner {result.winner!r} is not present in seat_groups")
    return groups


async def _apply_placement_scope_update(
    session: AsyncSession,
    *,
    game: Game,
    context: RatingContext,
    game_result: PlacementGameResult,
    agent_builds_by_seat: Mapping[str, uuid.UUID],
    model: PlackettLuce,
    now: datetime,
) -> list[PlacementRatingEvent]:
    """Run one multi-team placement update for the placement GLOBAL scope."""
    groups = _placement_groups_for(game_result)
    entries_by_group: dict[str, list[tuple[str, PlacementRating]]] = {group: [] for group in groups}
    group_by_build: dict[uuid.UUID, str] = {}
    for public_player_id in sorted(game_result.seat_groups):
        group = game_result.seat_groups[public_player_id]
        agent_build_id = agent_builds_by_seat[public_player_id]
        existing_group = group_by_build.get(agent_build_id)
        if existing_group is not None:
            if existing_group != group:
                msg = (
                    f"agent build {agent_build_id} maps to multiple placement outcome groups: "
                    f"{existing_group!r} and {group!r}"
                )
                raise ValueError(msg)
            continue
        group_by_build[agent_build_id] = group
        row = await placement_ratings_repo.get_or_create_placement_rating(
            session,
            rating_context_id=context.id,
            agent_build_id=agent_build_id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            initial_mu=INITIAL_MU,
            initial_sigma=INITIAL_SIGMA,
            initial_conservative_score=_conservative(INITIAL_MU, INITIAL_SIGMA),
        )
        entries_by_group[group].append((public_player_id, row))

    rating_teams = [
        [model.create_rating([row.mu, row.sigma], name=str(row.id)) for _sid, row in entries]
        for entries in (entries_by_group[group] for group in groups)
    ]
    is_draw = game_result.winner == PLACEMENT_DRAW
    ranks = [1.0 if is_draw or group == game_result.winner else 2.0 for group in groups]
    new_teams = model.rate(rating_teams, ranks=ranks)

    events: list[PlacementRatingEvent] = []
    for _group, entries, new_team in zip(
        groups,
        [entries_by_group[group] for group in groups],
        new_teams,
        strict=True,
    ):
        for (public_player_id, row), new in zip(entries, new_team, strict=True):
            events.append(
                await _persist_one_placement(
                    session,
                    row=row,
                    new_mu=row.mu if is_draw else new.mu,
                    new_sigma=new.sigma,
                    game_id=game.id,
                    rating_context_id=context.id,
                    game_seed=game.game_seed,
                    team_outcome=game_result.winner,
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    public_player_id=public_player_id,
                    now=now,
                )
            )
    return events


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
    metadata = await _resolve_canonical_metadata(
        session,
        league_id=league_id,
        game_id=game_result.game_id,
    )
    if metadata is None:
        return []
    game, context = metadata

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
            ruleset_id=game.ruleset_id,
            rating_context_id=context.id,
            game_seed=game.game_seed,
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
            ruleset_id=game.ruleset_id,
            rating_context_id=context.id,
            game_seed=game.game_seed,
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


async def update_placement_ratings_for_game(
    session: AsyncSession,
    *,
    game_result: PlacementGameResult,
    agent_builds_by_seat: Mapping[str, uuid.UUID],
    now: datetime | None = None,
) -> list[PlacementRatingEvent]:
    """Apply OpenSkill placement updates for one non-canonical game.

    The winning terminal group is ranked first and every other group ties for
    second place. Writes are restricted to the ``placement_ratings`` sibling
    tables for the game's exact non-canonical PLACEMENT context; missing,
    canonical, or otherwise malformed contexts return no events.
    """
    metadata = await _resolve_placement_metadata(session, game_id=game_result.game_id)
    if metadata is None:
        return []
    game, context = metadata

    _now = now if now is not None else datetime.now(UTC)
    model = PlackettLuce(mu=INITIAL_MU, sigma=INITIAL_SIGMA)
    return await _apply_placement_scope_update(
        session,
        game=game,
        context=context,
        game_result=game_result,
        agent_builds_by_seat=agent_builds_by_seat,
        model=model,
        now=_now,
    )


def _pair_sort_key(game: Game) -> tuple[str, int, str]:
    leg = game.pair_leg if game.pair_leg is not None else 0
    return (game.game_seed, leg, str(game.id))


async def _ordered_pair_results(
    session: AsyncSession,
    game_results: Sequence[PairedGameResult],
) -> list[PairedGameResult]:
    if len(game_results) != 2:
        raise ValueError(f"mirror pair rating requires exactly 2 legs, got {len(game_results)}")
    ids = [result.game_id for result in game_results]
    rows = (await session.execute(select(Game).where(Game.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    if set(by_id) != set(ids):
        missing = sorted(str(game_id) for game_id in set(ids) - set(by_id))
        raise ValueError(f"unknown paired game id(s): {missing}")
    return sorted(game_results, key=lambda result: _pair_sort_key(by_id[result.game_id]))


async def update_ratings_for_mirror_pair(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_results: Sequence[PairedGameResult],
    now: datetime | None = None,
) -> list[RatingEvent]:
    """Apply both legs of a mirror pair in persisted canonical order.

    Plackett-Luce updates are sequential, so this function canonicalizes leg
    order using ``(game_seed, pair_leg, game_id)`` before applying the two
    ordinary game updates inside the caller's transaction. Passing the same two
    legs in any input order therefore yields byte-identical rating rows.
    """
    ordered = await _ordered_pair_results(session, game_results)
    _now = now if now is not None else datetime.now(UTC)
    events: list[RatingEvent] = []
    for result in ordered:
        events.extend(
            await update_ratings_for_game(
                session,
                league_id=league_id,
                game_result=GameResult(
                    game_id=result.game_id,
                    winner=result.winner,
                    seat_factions=result.seat_factions,
                ),
                agent_builds_by_seat=result.agent_builds_by_seat,
                now=_now,
            )
        )
    return events


def _winner_from_game(game: Game) -> Literal["TOWN", "MAFIA", "DRAW"] | None:
    payload = game.terminal_result if isinstance(game.terminal_result, dict) else {}
    winner = payload.get("winner")
    if winner in {"TOWN", "MAFIA", "DRAW"}:
        return cast(Literal["TOWN", "MAFIA", "DRAW"], winner)
    return None


async def _existing_rating_event_for_games(
    session: AsyncSession,
    game_ids: Sequence[uuid.UUID],
) -> bool:
    row = (
        await session.execute(
            select(RatingEvent.id).where(RatingEvent.game_id.in_(game_ids)).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def update_ratings_for_completed_pair(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    pair_id: uuid.UUID,
    now: datetime | None = None,
) -> list[RatingEvent]:
    """Rate a completed mirror pair from persisted game/seat rows.

    Returns an empty list until both legs are completed, if the pair has already
    produced rating events, or if the pair is not fully AI-attributed.
    """
    games = list(
        (
            await session.execute(
                select(Game).where(Game.pair_id == pair_id).order_by(Game.pair_leg, Game.id)
            )
        )
        .scalars()
        .all()
    )
    if len(games) != 2 or any(game.status != "COMPLETED" for game in games):
        return []
    game_ids = [game.id for game in games]
    if await _existing_rating_event_for_games(session, game_ids):
        return []

    seats = list(
        (
            await session.execute(
                select(GameSeat).where(GameSeat.game_id.in_(game_ids)).order_by(GameSeat.seat_index)
            )
        )
        .scalars()
        .all()
    )
    seats_by_game: dict[uuid.UUID, list[GameSeat]] = {game_id: [] for game_id in game_ids}
    for seat in seats:
        seats_by_game.setdefault(seat.game_id, []).append(seat)

    pair_results: list[PairedGameResult] = []
    for game in games:
        winner = _winner_from_game(game)
        game_seats = seats_by_game.get(game.id, [])
        if (
            winner is None
            or not game_seats
            or any(seat.agent_build_id is None for seat in game_seats)
        ):
            return []
        pair_results.append(
            PairedGameResult(
                game_id=game.id,
                winner=winner,
                seat_factions={seat.public_player_id: Faction(seat.faction) for seat in game_seats},
                agent_builds_by_seat={
                    seat.public_player_id: cast(uuid.UUID, seat.agent_build_id)
                    for seat in game_seats
                },
            )
        )
    return await update_ratings_for_mirror_pair(
        session,
        league_id=league_id,
        game_results=pair_results,
        now=now,
    )


__all__ = [
    "INITIAL_MU",
    "INITIAL_SIGMA",
    "SCOPE_FACTION",
    "SCOPE_GLOBAL",
    "SCOPE_VALUE_GLOBAL",
    "GameResult",
    "PairedGameResult",
    "PlacementGameResult",
    "update_placement_ratings_for_game",
    "update_ratings_for_completed_pair",
    "update_ratings_for_game",
    "update_ratings_for_mirror_pair",
]
