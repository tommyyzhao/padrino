"""Gauntlet evaluation report (US-077).

Given a finalized (or in-progress) gauntlet, :func:`evaluate_gauntlet`
returns a :class:`GauntletReport` summarising:

* faction win counts + Wilson 95% confidence intervals,
* role-family breakdown (games, wins, draws, losses, Wilson CI),
* average days-to-terminal across completed games,
* average non-NOOP / non-ABSTAIN submission events per seat,
* per-agent_build rating deltas (pre / post mu and sigma).

The Wilson CI math lives in pure-core (``padrino.core.statistics``); this
module is part of the impure ``padrino.gauntlets`` layer because it reads
the database.

The report is identity-safe — every public-surfaced payload keys agent
performance only by ``agent_build_id``. The :func:`redact_for_public`
helper drops the model-identity columns when shipping to a public consumer
(``/public/gauntlets/{id}/report``), matching the existing privacy posture
from US-067.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction, Role, RoleFamily
from padrino.core.rulesets import get_ruleset
from padrino.core.statistics import ConfidenceInterval, wilson_score_interval
from padrino.db.models import (
    Game,
    GameEvent,
    GameSeat,
    Gauntlet,
    RatingEvent,
)

_COMPLETED_STATUS: Final[str] = "COMPLETED"
_GAME_TERMINATED_EVENT: Final[str] = "GameTerminated"
_DRAW_KEY: Final[str] = "DRAW"

# Submission events that count as a real (non-NOOP / non-ABSTAIN) action. A
# NOOP emits no event at all; an ABSTAIN emits ``VoteSubmitted`` with
# ``payload.is_abstain == True``. The four real action event types below are
# the canonical source per the wave-3 US-071 learnings.
_REAL_ACTION_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "MafiaKillVoteSubmitted",
        "ProtectSubmitted",
        "InvestigateSubmitted",
    }
)


class CIBand(BaseModel):
    """Pydantic mirror of :class:`padrino.core.statistics.ConfidenceInterval`."""

    model_config = ConfigDict(frozen=True)

    point: float
    lower: float
    upper: float


def _ci_band(ci: ConfidenceInterval) -> CIBand:
    return CIBand(point=ci.point, lower=ci.lower, upper=ci.upper)


class FactionWinRate(BaseModel):
    model_config = ConfigDict(frozen=True)

    faction: str
    wins: int
    games: int
    rate: CIBand


class RoleFamilyBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    role_family: str
    games: int
    wins: int
    draws: int
    losses: int
    win_rate: CIBand


class ModelSeatCounts(BaseModel):
    """How many TOWN vs MAFIA seats one model occupied across the gauntlet (US-084)."""

    model_config = ConfigDict(frozen=True)

    agent_build_id: uuid.UUID
    town_seats: int
    mafia_seats: int
    total_seats: int


class ModelFactionWinRate(BaseModel):
    """Per-(model, faction) win rate with a Wilson CI band (US-084).

    ``seats`` is the number of games this model played in ``faction``; ``wins``
    is how many of those that faction won (draws count as non-wins).
    """

    model_config = ConfigDict(frozen=True)

    agent_build_id: uuid.UUID
    faction: str
    seats: int
    wins: int
    win_rate: CIBand


class RatingDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_build_id: uuid.UUID
    scope_type: str
    scope_value: str
    games_in_gauntlet: int
    pre_mu: float
    pre_sigma: float
    post_mu: float
    post_sigma: float
    delta_mu: float
    delta_sigma: float


class GauntletReport(BaseModel):
    """Structured evaluation report for a gauntlet."""

    model_config = ConfigDict(frozen=True)

    gauntlet_id: uuid.UUID
    status: str
    ruleset_id: str
    clone_count: int
    games_total: int
    games_completed: int
    faction_win_counts: dict[str, int] = Field(default_factory=dict)
    faction_win_rates: list[FactionWinRate] = Field(default_factory=list)
    role_family_breakdown: list[RoleFamilyBreakdown] = Field(default_factory=list)
    faction_seat_counts: list[ModelSeatCounts] = Field(default_factory=list)
    model_faction_breakdown: list[ModelFactionWinRate] = Field(default_factory=list)
    average_days_to_terminal: float
    average_actions_per_seat: float
    rating_deltas: list[RatingDelta] = Field(default_factory=list)


# Keys that identify a model in a `RatingDelta`-shaped payload but are not
# present today — kept here as a forward-compatible scrub set. The current
# `RatingDelta` schema already publishes only `agent_build_id`; the public
# projection drops nothing extra, but future schema additions that include
# `model_provider` / `model_name` / `model_version` are stripped by this set
# without having to update every public route.
_PUBLIC_RATING_DELTA_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model_provider",
        "model_name",
        "model_version",
        "provider",
        "display_name",
    }
)


def _role_family(role_str: str, ruleset: Any) -> RoleFamily | None:
    try:
        role = Role(role_str)
    except ValueError:
        return None
    return cast(RoleFamily, ruleset.role_family_for(role))


def _faction_for_role(role_str: str, ruleset: Any) -> Faction | None:
    try:
        role = Role(role_str)
    except ValueError:
        return None
    return cast(Faction, ruleset.faction_for(role))


def _winner_from_terminal(terminal_result: Any) -> str | None:
    if not isinstance(terminal_result, dict):
        return None
    winner = terminal_result.get("winner")
    if not isinstance(winner, str):
        return None
    return winner


def _day_terminated(terminal_result: Any) -> int | None:
    if not isinstance(terminal_result, dict):
        return None
    day = terminal_result.get("day_terminated")
    if isinstance(day, bool):  # bool is a subclass of int — exclude explicitly
        return None
    if isinstance(day, int):
        return day
    return None


async def _games_for_gauntlet(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
) -> list[Game]:
    stmt = select(Game).where(Game.gauntlet_id == gauntlet_id).order_by(Game.created_at, Game.id)
    return list((await session.execute(stmt)).scalars().all())


async def _seats_by_game(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[GameSeat]]:
    ids = list(game_ids)
    by_game: dict[uuid.UUID, list[GameSeat]] = {gid: [] for gid in ids}
    if not ids:
        return by_game
    stmt = select(GameSeat).where(GameSeat.game_id.in_(ids))
    for seat in (await session.execute(stmt)).scalars().all():
        by_game.setdefault(seat.game_id, []).append(seat)
    return by_game


async def _real_action_event_count(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> int:
    ids = list(game_ids)
    if not ids:
        return 0
    # VoteSubmitted with is_abstain=False is treated as a real action, while
    # is_abstain=True is not. Pull the payload so we can filter accurately.
    stmt = select(GameEvent.event_type, GameEvent.payload).where(
        GameEvent.game_id.in_(ids),
        GameEvent.event_type.in_({*_REAL_ACTION_EVENT_TYPES, "VoteSubmitted"}),
    )
    count = 0
    for event_type, payload in (await session.execute(stmt)).all():
        if event_type == "VoteSubmitted":
            if isinstance(payload, dict) and payload.get("is_abstain") is True:
                continue
            count += 1
        else:
            count += 1
    return count


def _rating_deltas_from_events(
    events: Iterable[RatingEvent],
) -> list[RatingDelta]:
    """Collapse a stream of per-game RatingEvent rows into per-(build,scope) deltas.

    For each ``(agent_build_id, scope_type, scope_value)`` we take the first
    ``before_mu`` / ``before_sigma`` (chronologically) as the pre values and
    the last ``after_mu`` / ``after_sigma`` as the post values. ``RatingEvent``
    rows are already ordered by ``created_at, id`` from the repository, so we
    rely on the caller to preserve that order.
    """
    grouped: dict[tuple[uuid.UUID, str, str], list[RatingEvent]] = {}
    for ev in events:
        key = (ev.agent_build_id, ev.scope_type, ev.scope_value)
        grouped.setdefault(key, []).append(ev)

    deltas: list[RatingDelta] = []
    for (ab_id, scope_type, scope_value), rows in grouped.items():
        first = rows[0]
        last = rows[-1]
        deltas.append(
            RatingDelta(
                agent_build_id=ab_id,
                scope_type=scope_type,
                scope_value=scope_value,
                games_in_gauntlet=len(rows),
                pre_mu=first.before_mu,
                pre_sigma=first.before_sigma,
                post_mu=last.after_mu,
                post_sigma=last.after_sigma,
                delta_mu=last.after_mu - first.before_mu,
                delta_sigma=last.after_sigma - first.before_sigma,
            )
        )
    deltas.sort(key=lambda d: (str(d.agent_build_id), d.scope_type, d.scope_value))
    return deltas


async def _rating_events_for_games(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> list[RatingEvent]:
    ids = list(game_ids)
    if not ids:
        return []
    stmt = (
        select(RatingEvent)
        .where(RatingEvent.game_id.in_(ids))
        .order_by(RatingEvent.created_at, RatingEvent.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def evaluate_gauntlet(
    gauntlet_id: uuid.UUID,
    session: AsyncSession,
) -> GauntletReport | None:
    """Return the :class:`GauntletReport` for ``gauntlet_id`` or ``None``.

    Returns ``None`` when the gauntlet row does not exist. Otherwise returns
    a report that reflects whatever state the gauntlet is in — partial
    gauntlets surface ``games_completed < games_total`` and CIs widen
    accordingly.
    """
    gauntlet = await session.get(Gauntlet, gauntlet_id)
    if gauntlet is None:
        return None

    games = await _games_for_gauntlet(session, gauntlet_id)
    games_total = len(games)
    completed_games = [g for g in games if g.status == _COMPLETED_STATUS]
    games_completed = len(completed_games)
    completed_ids = [g.id for g in completed_games]

    ruleset = get_ruleset(gauntlet.ruleset_id)

    seats_by_game = await _seats_by_game(session, completed_ids)
    total_seat_rows = sum(len(seats) for seats in seats_by_game.values())

    faction_wins: dict[str, int] = {
        Faction.TOWN.value: 0,
        Faction.MAFIA.value: 0,
        _DRAW_KEY: 0,
    }
    role_family_counters: dict[RoleFamily, dict[str, int]] = {
        rf: {"games": 0, "wins": 0, "draws": 0, "losses": 0} for rf in RoleFamily
    }
    # Per-model seat exposure (US-084) and per-(model, faction) win tallies.
    model_seat_counts: dict[uuid.UUID, dict[str, int]] = {}
    model_faction_tally: dict[tuple[uuid.UUID, str], dict[str, int]] = {}

    days_sum = 0
    days_sample_size = 0
    for game in completed_games:
        winner = _winner_from_terminal(game.terminal_result)
        if winner == Faction.TOWN.value:
            faction_wins[Faction.TOWN.value] += 1
        elif winner == Faction.MAFIA.value:
            faction_wins[Faction.MAFIA.value] += 1
        else:
            faction_wins[_DRAW_KEY] += 1

        day = _day_terminated(game.terminal_result)
        if day is not None:
            days_sum += day
            days_sample_size += 1

        for seat in seats_by_game.get(game.id, []):
            rf = _role_family(seat.role, ruleset)
            faction = _faction_for_role(seat.role, ruleset)
            if rf is None or faction is None:
                continue
            counter = role_family_counters[rf]
            counter["games"] += 1
            if winner is None or winner == _DRAW_KEY:
                counter["draws"] += 1
            elif winner == faction.value:
                counter["wins"] += 1
            else:
                counter["losses"] += 1

            build_id = seat.agent_build_id
            if build_id is not None:
                seat_counts = model_seat_counts.setdefault(build_id, {"TOWN": 0, "MAFIA": 0})
                seat_counts[faction.value] += 1
                tally = model_faction_tally.setdefault(
                    (build_id, faction.value), {"games": 0, "wins": 0}
                )
                tally["games"] += 1
                if winner is not None and winner != _DRAW_KEY and winner == faction.value:
                    tally["wins"] += 1

    faction_win_rates = [
        FactionWinRate(
            faction=Faction.TOWN.value,
            wins=faction_wins[Faction.TOWN.value],
            games=games_completed,
            rate=_ci_band(wilson_score_interval(faction_wins[Faction.TOWN.value], games_completed)),
        ),
        FactionWinRate(
            faction=Faction.MAFIA.value,
            wins=faction_wins[Faction.MAFIA.value],
            games=games_completed,
            rate=_ci_band(
                wilson_score_interval(faction_wins[Faction.MAFIA.value], games_completed)
            ),
        ),
        FactionWinRate(
            faction=_DRAW_KEY,
            wins=faction_wins[_DRAW_KEY],
            games=games_completed,
            rate=_ci_band(wilson_score_interval(faction_wins[_DRAW_KEY], games_completed)),
        ),
    ]

    role_family_breakdown = []
    for rf in RoleFamily:
        c = role_family_counters[rf]
        role_family_breakdown.append(
            RoleFamilyBreakdown(
                role_family=rf.value,
                games=c["games"],
                wins=c["wins"],
                draws=c["draws"],
                losses=c["losses"],
                win_rate=_ci_band(wilson_score_interval(c["wins"], c["games"])),
            )
        )

    faction_seat_counts = [
        ModelSeatCounts(
            agent_build_id=bid,
            town_seats=counts["TOWN"],
            mafia_seats=counts["MAFIA"],
            total_seats=counts["TOWN"] + counts["MAFIA"],
        )
        for bid, counts in model_seat_counts.items()
    ]
    faction_seat_counts.sort(key=lambda m: str(m.agent_build_id))

    model_faction_breakdown = [
        ModelFactionWinRate(
            agent_build_id=bid,
            faction=faction_value,
            seats=tally["games"],
            wins=tally["wins"],
            win_rate=_ci_band(wilson_score_interval(tally["wins"], tally["games"])),
        )
        for (bid, faction_value), tally in model_faction_tally.items()
    ]
    model_faction_breakdown.sort(key=lambda m: (str(m.agent_build_id), m.faction))

    real_action_count = await _real_action_event_count(session, completed_ids)
    average_actions_per_seat = real_action_count / total_seat_rows if total_seat_rows > 0 else 0.0
    average_days = (days_sum / days_sample_size) if days_sample_size > 0 else 0.0

    rating_events = await _rating_events_for_games(session, completed_ids)
    rating_deltas = _rating_deltas_from_events(rating_events)

    return GauntletReport(
        gauntlet_id=gauntlet_id,
        status=gauntlet.status,
        ruleset_id=gauntlet.ruleset_id,
        clone_count=gauntlet.clone_count,
        games_total=games_total,
        games_completed=games_completed,
        faction_win_counts=dict(faction_wins),
        faction_win_rates=faction_win_rates,
        role_family_breakdown=role_family_breakdown,
        faction_seat_counts=faction_seat_counts,
        model_faction_breakdown=model_faction_breakdown,
        average_days_to_terminal=average_days,
        average_actions_per_seat=average_actions_per_seat,
        rating_deltas=rating_deltas,
    )


def redact_for_public(report: GauntletReport) -> dict[str, Any]:
    """Return ``report.model_dump()`` with model-identity columns scrubbed.

    The current ``RatingDelta`` schema already publishes only
    ``agent_build_id`` (no model_provider / model_name / model_version), so
    this helper is defense-in-depth against a future field addition. Today
    it leaves the payload structurally unchanged but strips any forbidden
    keys that might appear via ``model_dump(by_alias=True, exclude=None)``.
    """
    raw: dict[str, Any] = report.model_dump(mode="json")
    deltas = raw.get("rating_deltas", [])
    if isinstance(deltas, list):
        scrubbed: list[Mapping[str, Any]] = []
        for entry in deltas:
            if isinstance(entry, dict):
                scrubbed.append(
                    {k: v for k, v in entry.items() if k not in _PUBLIC_RATING_DELTA_FORBIDDEN_KEYS}
                )
            else:
                scrubbed.append(entry)
        raw["rating_deltas"] = scrubbed
    return raw


__all__ = [
    "CIBand",
    "FactionWinRate",
    "GauntletReport",
    "ModelFactionWinRate",
    "ModelSeatCounts",
    "RatingDelta",
    "RoleFamilyBreakdown",
    "evaluate_gauntlet",
    "redact_for_public",
]
