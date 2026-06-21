"""Per-league leaderboard aggregation (US-045 / prd.md §10.4).

Reads ``game_seats``, ``game_events``, ``ratings``, and ``agent_builds`` to
build the response contract for ``GET /leagues/{id}/leaderboard``. The
provisional flag reuses the thresholds from :mod:`padrino.gauntlets.completion`
so a build that has cleared them here is consistent with how a freshly
finalized gauntlet would report it.

This module is in the impure service layer; it is free to use SQLAlchemy and
wall-clock and is NOT subject to the pure-core firewall.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction, Role
from padrino.core.rulesets import get_ruleset
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    Gauntlet,
    PromptVersion,
    Rating,
)
from padrino.gauntlets.completion import (
    PROVISIONAL_MAFIA_GAMES,
    PROVISIONAL_TOTAL_GAMES,
    PROVISIONAL_TOWN_GAMES,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_FACTION,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)

RATING_MODEL: Final[str] = "openskill_plackett_luce_v1"

_TERMINATED_EVENT_TYPE: Final[str] = "GameTerminated"
_PUBLIC_MESSAGE_EVENT_TYPE: Final[str] = "PublicMessageSubmitted"
_TIMEOUT_EVENT_TYPE: Final[str] = "ActionTimedOut"
_INVALID_EVENT_TYPE: Final[str] = "OutputInvalid"

_SUBMISSION_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "PublicMessageSubmitted",
        "PrivateMessageSubmitted",
        "VoteSubmitted",
        "MafiaKillVoteSubmitted",
        "ProtectSubmitted",
        "InvestigateSubmitted",
        "ActionTimedOut",
        "OutputInvalid",
        "OutputTruncated",
    }
)


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One row in the leaderboard response, per (league, agent_build)."""

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
    faction_breakdown: dict[str, dict[str, float]]
    role_breakdown: dict[str, dict[str, float]]
    role_family_breakdown: dict[str, dict[str, float]]
    provisional: bool


@dataclass(frozen=True, slots=True)
class Leaderboard:
    """Top-level leaderboard payload returned by the API."""

    leaderboard_id: str
    ruleset_id: str
    prompt_version: str
    rating_model: str
    entries: list[LeaderboardEntry]


def _is_provisional(total: int, town: int, mafia: int) -> bool:
    return (
        total < PROVISIONAL_TOTAL_GAMES
        or mafia < PROVISIONAL_MAFIA_GAMES
        or town < PROVISIONAL_TOWN_GAMES
    )


async def _terminal_games_in_league(
    session: AsyncSession,
    league_id: uuid.UUID,
    gauntlet_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, str]:
    """Return ``{game_id: winner}`` for every terminal game under ``league_id``.

    When ``gauntlet_id`` is provided the result is narrowed to that one
    gauntlet — used by the leaderboard route to scope a query to a single
    bracket without recomputing the full league.
    """
    stmt = (
        select(Game.id, GameEvent.payload)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .join(GameEvent, GameEvent.game_id == Game.id)
        .where(
            Gauntlet.league_id == league_id,
            GameEvent.event_type == _TERMINATED_EVENT_TYPE,
        )
    )
    if gauntlet_id is not None:
        stmt = stmt.where(Game.gauntlet_id == gauntlet_id)
    out: dict[uuid.UUID, str] = {}
    for game_id, payload in (await session.execute(stmt)).all():
        winner = payload.get("winner") if isinstance(payload, dict) else None
        if isinstance(winner, str):
            out[game_id] = winner
    return out


async def _seats_for_games(session: AsyncSession, game_ids: Iterable[uuid.UUID]) -> list[GameSeat]:
    ids = list(game_ids)
    if not ids:
        return []
    stmt = select(GameSeat).where(GameSeat.game_id.in_(ids))
    return list((await session.execute(stmt)).scalars().all())


async def _ratings_by_scope(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[tuple[uuid.UUID, str, str], Rating]:
    ids = list(agent_build_ids)
    if not ids:
        return {}
    stmt = select(Rating).where(
        Rating.league_id == league_id,
        Rating.agent_build_id.in_(ids),
    )
    return {
        (rating.agent_build_id, rating.scope_type, rating.scope_value): rating
        for rating in (await session.execute(stmt)).scalars().all()
    }


async def _agent_build_display_names(
    session: AsyncSession,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, str]:
    ids = list(agent_build_ids)
    if not ids:
        return {}
    stmt = select(AgentBuild.id, AgentBuild.display_name).where(AgentBuild.id.in_(ids))
    return {row[0]: row[1] for row in (await session.execute(stmt)).all()}


async def _league_prompt_version(session: AsyncSession, league_id: uuid.UUID) -> str:
    """Return the ``PromptVersion.version`` of the league's first gauntlet, or ``""``."""
    stmt = (
        select(PromptVersion.version)
        .join(Gauntlet, Gauntlet.prompt_version_id == PromptVersion.id)
        .where(Gauntlet.league_id == league_id)
        .order_by(Gauntlet.created_at, Gauntlet.id)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return ""
    return str(row[0])


async def _events_for_games(
    session: AsyncSession, game_ids: Iterable[uuid.UUID]
) -> list[GameEvent]:
    ids = list(game_ids)
    if not ids:
        return []
    stmt = select(GameEvent).where(
        GameEvent.game_id.in_(ids),
        GameEvent.event_type.in_(_SUBMISSION_EVENT_TYPES),
    )
    return list((await session.execute(stmt)).scalars().all())


def _per_ab_counters(
    seats: list[GameSeat],
    winners: dict[uuid.UUID, str],
) -> dict[uuid.UUID, dict[str, int]]:
    """Bucket seats by agent_build, counting games / wins / draws / losses + factions."""
    counters: dict[uuid.UUID, dict[str, int]] = {}
    for seat in seats:
        # ``agent_build_id`` is nullable since Wave 9 (human seats); the
        # scientific leaderboard only aggregates AI seats.
        if seat.agent_build_id is None:
            continue
        bucket = counters.setdefault(
            seat.agent_build_id,
            {
                "games": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "town_games": 0,
                "mafia_games": 0,
            },
        )
        bucket["games"] += 1
        if seat.faction == Faction.TOWN.value:
            bucket["town_games"] += 1
        elif seat.faction == Faction.MAFIA.value:
            bucket["mafia_games"] += 1
        winner = winners.get(seat.game_id)
        if winner == "DRAW":
            bucket["draws"] += 1
        elif winner == seat.faction:
            bucket["wins"] += 1
        elif winner is not None:
            bucket["losses"] += 1
    return counters


def _empty_counter() -> dict[str, float]:
    return {"games": 0.0, "wins": 0.0, "draws": 0.0, "losses": 0.0, "win_rate": 0.0}


def _finalize_rate_counters(
    counters: dict[uuid.UUID, dict[str, dict[str, float]]],
) -> dict[uuid.UUID, dict[str, dict[str, float]]]:
    for ab_bucket in counters.values():
        for bucket in ab_bucket.values():
            games = bucket["games"]
            bucket["win_rate"] = (bucket["wins"] / games) if games else 0.0
    return counters


def _per_ab_faction_breakdown(
    seats: list[GameSeat],
    winners: dict[uuid.UUID, str],
) -> dict[uuid.UUID, dict[str, dict[str, float]]]:
    """Aggregate exact per-faction seat-game counters by agent build."""
    out: dict[uuid.UUID, dict[str, dict[str, float]]] = {}
    for seat in seats:
        if seat.agent_build_id is None or seat.faction not in {
            Faction.TOWN.value,
            Faction.MAFIA.value,
        }:
            continue
        ab_bucket = out.setdefault(seat.agent_build_id, {})
        faction_bucket = ab_bucket.setdefault(seat.faction, _empty_counter())
        faction_bucket["games"] += 1
        winner = winners.get(seat.game_id)
        if winner == "DRAW":
            faction_bucket["draws"] += 1
        elif winner == seat.faction:
            faction_bucket["wins"] += 1
        elif winner is not None:
            faction_bucket["losses"] += 1
    return _finalize_rate_counters(out)


def _per_ab_role_breakdown(
    seats: list[GameSeat],
    winners: dict[uuid.UUID, str],
) -> dict[uuid.UUID, dict[str, dict[str, float]]]:
    """Aggregate exact per-role seat-game counters by agent build."""
    out: dict[uuid.UUID, dict[str, dict[str, float]]] = {}
    for seat in seats:
        if seat.agent_build_id is None:
            continue
        try:
            role = Role(seat.role)
        except ValueError:
            continue
        ab_bucket = out.setdefault(seat.agent_build_id, {})
        role_bucket = ab_bucket.setdefault(role.value, _empty_counter())
        role_bucket["games"] += 1
        winner = winners.get(seat.game_id)
        if winner == "DRAW":
            role_bucket["draws"] += 1
        elif winner == seat.faction:
            role_bucket["wins"] += 1
        elif winner is not None:
            role_bucket["losses"] += 1
    return _finalize_rate_counters(out)


def _per_ab_role_family_breakdown(
    seats: list[GameSeat],
    winners: dict[uuid.UUID, str],
    ruleset: Any,
) -> dict[uuid.UUID, dict[str, dict[str, float]]]:
    """Aggregate per-(agent_build, role_family) seat-game counters.

    Returns a mapping ``{agent_build_id: {role_family.value: {games, wins,
    draws, losses, win_rate}}}``. ``win_rate`` is ``wins / games`` (0.0 when
    ``games == 0``). Seats whose role string is not a valid ``Role`` enum
    member are skipped silently — they should not occur in practice but the
    leaderboard route stays robust against historical rows.
    """
    out: dict[uuid.UUID, dict[str, dict[str, float]]] = {}
    for seat in seats:
        if seat.agent_build_id is None:
            continue
        try:
            role = Role(seat.role)
        except ValueError:
            continue
        family = ruleset.role_family_for(role).value
        ab_bucket = out.setdefault(seat.agent_build_id, {})
        rf_bucket = ab_bucket.setdefault(
            family, {"games": 0.0, "wins": 0.0, "draws": 0.0, "losses": 0.0, "win_rate": 0.0}
        )
        rf_bucket["games"] += 1
        winner = winners.get(seat.game_id)
        if winner == "DRAW":
            rf_bucket["draws"] += 1
        elif winner == seat.faction:
            rf_bucket["wins"] += 1
        elif winner is not None:
            rf_bucket["losses"] += 1
    for ab_bucket in out.values():
        for rf_bucket in ab_bucket.values():
            games = rf_bucket["games"]
            rf_bucket["win_rate"] = (rf_bucket["wins"] / games) if games else 0.0
    return out


def _per_ab_event_metrics(
    events: list[GameEvent],
    seat_by_game_actor: dict[tuple[uuid.UUID, str], uuid.UUID],
) -> dict[uuid.UUID, dict[str, float]]:
    """Aggregate timeout / invalid rates and public_message_avg_chars per AB."""
    metrics: dict[uuid.UUID, dict[str, float]] = {}
    for event in events:
        actor = event.actor_player_id
        if actor is None:
            continue
        ab_id = seat_by_game_actor.get((event.game_id, actor))
        if ab_id is None:
            continue
        bucket = metrics.setdefault(
            ab_id,
            {
                "submissions": 0.0,
                "timeouts": 0.0,
                "invalids": 0.0,
                "pm_count": 0.0,
                "pm_chars": 0.0,
            },
        )
        bucket["submissions"] += 1
        if event.event_type == _TIMEOUT_EVENT_TYPE:
            bucket["timeouts"] += 1
        elif event.event_type == _INVALID_EVENT_TYPE:
            bucket["invalids"] += 1
        if event.event_type == _PUBLIC_MESSAGE_EVENT_TYPE and isinstance(event.payload, dict):
            text = event.payload.get("text")
            if isinstance(text, str):
                bucket["pm_count"] += 1
                bucket["pm_chars"] += len(text)
    return metrics


def _build_entry(
    *,
    ab_id: uuid.UUID,
    display_name: str,
    counts: dict[str, int],
    metrics: dict[str, float] | None,
    rating: Rating | None,
    faction_breakdown: dict[str, dict[str, float]],
    faction_ratings: dict[str, Rating],
    role_breakdown: dict[str, dict[str, float]],
    role_family_breakdown: dict[str, dict[str, float]],
) -> LeaderboardEntry:
    submissions = (metrics or {}).get("submissions", 0.0)
    timeouts = (metrics or {}).get("timeouts", 0.0)
    invalids = (metrics or {}).get("invalids", 0.0)
    pm_count = (metrics or {}).get("pm_count", 0.0)
    pm_chars = (metrics or {}).get("pm_chars", 0.0)

    timeout_rate = (timeouts / submissions) if submissions else 0.0
    invalid_rate = (invalids / submissions) if submissions else 0.0
    pm_avg = (pm_chars / pm_count) if pm_count else 0.0

    if rating is not None:
        mu = float(rating.mu)
        sigma = float(rating.sigma)
        cs = float(rating.conservative_score)
    else:
        mu = INITIAL_MU
        sigma = INITIAL_SIGMA
        cs = INITIAL_MU - 3.0 * INITIAL_SIGMA

    decorated_factions: dict[str, dict[str, float]] = {}
    for faction in (Faction.TOWN.value, Faction.MAFIA.value):
        stats = dict(faction_breakdown.get(faction, _empty_counter()))
        faction_rating = faction_ratings.get(faction)
        if faction_rating is not None:
            stats["mu"] = float(faction_rating.mu)
            stats["sigma"] = float(faction_rating.sigma)
            stats["conservative_score"] = float(faction_rating.conservative_score)
        else:
            stats["mu"] = INITIAL_MU
            stats["sigma"] = INITIAL_SIGMA
            stats["conservative_score"] = INITIAL_MU - 3.0 * INITIAL_SIGMA
        decorated_factions[faction] = stats

    return LeaderboardEntry(
        agent_build_id=ab_id,
        display_name=display_name,
        games=counts["games"],
        wins=counts["wins"],
        draws=counts["draws"],
        losses=counts["losses"],
        mu=mu,
        sigma=sigma,
        conservative_score=cs,
        timeout_rate=timeout_rate,
        invalid_action_rate=invalid_rate,
        public_message_avg_chars=pm_avg,
        faction_breakdown=decorated_factions,
        role_breakdown=role_breakdown,
        role_family_breakdown=role_family_breakdown,
        provisional=_is_provisional(counts["games"], counts["town_games"], counts["mafia_games"]),
    )


async def compute_leaderboard(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    gauntlet_id: uuid.UUID | None = None,
) -> Leaderboard:
    """Aggregate the per-AB leaderboard rows for one league.

    The caller is responsible for resolving the league row and verifying it
    exists; this helper accepts the ruleset id directly so it can run in the
    same transaction. ``gauntlet_id`` scopes the aggregation to one gauntlet
    bracket inside the league.
    """
    winners = await _terminal_games_in_league(session, league_id, gauntlet_id=gauntlet_id)
    seats = await _seats_for_games(session, winners.keys())
    events = await _events_for_games(session, winners.keys())

    seat_by_game_actor: dict[tuple[uuid.UUID, str], uuid.UUID] = {
        (seat.game_id, seat.public_player_id): seat.agent_build_id
        for seat in seats
        if seat.agent_build_id is not None
    }
    ruleset = get_ruleset(ruleset_id)
    counters = _per_ab_counters(seats, winners)
    metrics = _per_ab_event_metrics(events, seat_by_game_actor)
    faction_breakdowns = _per_ab_faction_breakdown(seats, winners)
    role_breakdowns = _per_ab_role_breakdown(seats, winners)
    role_family_breakdowns = _per_ab_role_family_breakdown(seats, winners, ruleset)

    ratings_by_scope = await _ratings_by_scope(
        session, league_id=league_id, agent_build_ids=counters.keys()
    )
    ratings = {
        ab_id: rating
        for (ab_id, scope_type, scope_value), rating in ratings_by_scope.items()
        if scope_type == SCOPE_GLOBAL and scope_value == SCOPE_VALUE_GLOBAL
    }
    display_names = await _agent_build_display_names(session, counters.keys())
    prompt_version = await _league_prompt_version(session, league_id)

    entries = [
        _build_entry(
            ab_id=ab_id,
            display_name=display_names.get(ab_id, ""),
            counts=counts,
            metrics=metrics.get(ab_id),
            rating=ratings.get(ab_id),
            faction_breakdown=faction_breakdowns.get(ab_id, {}),
            faction_ratings={
                faction: rating
                for (rating_ab, scope_type, faction), rating in ratings_by_scope.items()
                if rating_ab == ab_id and scope_type == SCOPE_FACTION
            },
            role_breakdown=role_breakdowns.get(ab_id, {}),
            role_family_breakdown=role_family_breakdowns.get(ab_id, {}),
        )
        for ab_id, counts in counters.items()
    ]
    entries.sort(key=lambda e: (-e.conservative_score, str(e.agent_build_id)))

    return Leaderboard(
        leaderboard_id=f"lb_{uuid.uuid4().hex}",
        ruleset_id=ruleset_id,
        prompt_version=prompt_version,
        rating_model=RATING_MODEL,
        entries=entries,
    )


def entry_to_response(entry: LeaderboardEntry) -> dict[str, Any]:
    """Serialize one entry for the FastAPI response."""
    return {
        "agent_build_id": str(entry.agent_build_id),
        "display_name": entry.display_name,
        "games": entry.games,
        "wins": entry.wins,
        "draws": entry.draws,
        "losses": entry.losses,
        "mu": entry.mu,
        "sigma": entry.sigma,
        "conservative_score": entry.conservative_score,
        "timeout_rate": entry.timeout_rate,
        "invalid_action_rate": entry.invalid_action_rate,
        "public_message_avg_chars": entry.public_message_avg_chars,
        "faction_breakdown": entry.faction_breakdown,
        "role_breakdown": entry.role_breakdown,
        "role_family_breakdown": entry.role_family_breakdown,
        "provisional": entry.provisional,
    }


__all__ = [
    "RATING_MODEL",
    "Leaderboard",
    "LeaderboardEntry",
    "compute_leaderboard",
    "entry_to_response",
]
