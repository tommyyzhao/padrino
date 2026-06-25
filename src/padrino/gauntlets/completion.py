"""Gauntlet completion + leaderboard provisional-flag logic.

Once every child game of a gauntlet has reached a terminal row status and the
successful child games satisfy the faction-balance gate,
:func:`finalize_gauntlet_if_done` flips the gauntlet status to ``COMPLETED``,
stamps ``completed_at``, and returns aggregate diagnostics plus the provisional
flag for each agent build that played in the gauntlet.

Provisional thresholds (per ``prd.md`` §10):

* ``total_games >= 30``
* ``mafia_games >= 5``
* ``town_games >= 15``

Counts are league-scoped: per-agent_build totals span every terminal game
under the gauntlet's league, not just the gauntlet's own clones, so the
leaderboard view of "enough games played" is consistent across gauntlets.

Aggregate diagnostics are scoped to the gauntlet's successful child games only.

This module sits in the impure ``padrino.gauntlets`` layer and is therefore
permitted to read the wall clock for ``completed_at``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction
from padrino.core.rulesets import get_ruleset
from padrino.db.game_status import (
    GAME_STATUS_COMPLETED,
    GAME_STATUS_FAILED,
    is_terminal_game_status,
)
from padrino.db.models import Game, GameEvent, GameSeat, Gauntlet
from padrino.diagnostics.submissions import (
    INVALID_EVENT_TYPE,
    PUBLIC_MESSAGE_EVENT_TYPE,
    SUBMISSION_EVENT_TYPES,
    TIMEOUT_EVENT_TYPE,
)
from padrino.gauntlets.evaluation import ModelSeatCounts

PROVISIONAL_TOTAL_GAMES: Final[int] = 30
PROVISIONAL_MAFIA_GAMES: Final[int] = 5
PROVISIONAL_TOWN_GAMES: Final[int] = 15
DEFAULT_BALANCE_TOLERANCE_SEATS: Final[int] = 4

_COMPLETED_STATUS: Final[str] = GAME_STATUS_COMPLETED
_FAILED_STATUS: Final[str] = GAME_STATUS_FAILED


@dataclass(frozen=True, slots=True)
class TerminalProgress:
    """Generic ``done of total`` progress over terminal rows."""

    done: int
    total: int


@dataclass(frozen=True, slots=True)
class AgentBuildProvisional:
    """Per-agent_build leaderboard counters + provisional flag."""

    agent_build_id: uuid.UUID
    total_games: int
    town_games: int
    mafia_games: int
    provisional: bool


@dataclass(frozen=True, slots=True)
class GauntletDiagnostics:
    """Aggregate health metrics over a gauntlet's child games."""

    games_completed: int
    timeout_rate: float
    invalid_action_rate: float
    average_public_message_chars: float


@dataclass(frozen=True, slots=True)
class GauntletFinalized:
    """Result of a successful :func:`finalize_gauntlet_if_done` call."""

    gauntlet_id: uuid.UUID
    status: str
    completed_at: datetime
    provisional_by_agent_build: Mapping[uuid.UUID, AgentBuildProvisional]
    diagnostics: GauntletDiagnostics


def _is_provisional(total: int, town: int, mafia: int) -> bool:
    return (
        total < PROVISIONAL_TOTAL_GAMES
        or mafia < PROVISIONAL_MAFIA_GAMES
        or town < PROVISIONAL_TOWN_GAMES
    )


async def _all_games_terminal(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> bool:
    ids = list(game_ids)
    if not ids:
        return False
    stmt = select(Game.id, Game.status).where(Game.id.in_(ids))
    rows = list((await session.execute(stmt)).all())
    if {row[0] for row in rows} != set(ids):
        return False
    return all(is_terminal_game_status(str(status)) for _game_id, status in rows)


async def gauntlet_child_progress(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
) -> TerminalProgress | None:
    """Return terminal-child progress for one gauntlet, or ``None`` if unknown."""
    gauntlet = await session.get(Gauntlet, gauntlet_id)
    if gauntlet is None:
        return None
    statuses = list(
        (await session.execute(select(Game.status).where(Game.gauntlet_id == gauntlet_id)))
        .scalars()
        .all()
    )
    return TerminalProgress(
        done=sum(1 for status in statuses if is_terminal_game_status(str(status))),
        total=len(statuses),
    )


async def _child_game_statuses(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, str]:
    ids = list(game_ids)
    if not ids:
        return {}
    stmt = select(Game.id, Game.status).where(Game.id.in_(ids))
    return {game_id: str(status) for game_id, status in (await session.execute(stmt)).all()}


async def _completed_game_ids(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> list[uuid.UUID]:
    ids = list(game_ids)
    if not ids:
        return []
    stmt = select(Game.id).where(
        Game.id.in_(ids),
        Game.status == _COMPLETED_STATUS,
    )
    return list((await session.execute(stmt)).scalars().all())


async def _league_terminal_game_ids(
    session: AsyncSession,
    league_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return ids of every terminal game whose gauntlet belongs to ``league_id``."""
    stmt = (
        select(Game.id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(
            Gauntlet.league_id == league_id,
            Game.status == _COMPLETED_STATUS,
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def _provisional_for_agent_builds(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, AgentBuildProvisional]:
    abs_ = list(agent_build_ids)
    if not abs_:
        return {}
    terminal_ids = await _league_terminal_game_ids(session, league_id)
    counters: dict[uuid.UUID, dict[str, int]] = {
        ab_id: {"total": 0, "town": 0, "mafia": 0} for ab_id in abs_
    }
    if terminal_ids:
        stmt = select(GameSeat.agent_build_id, GameSeat.faction).where(
            GameSeat.agent_build_id.in_(abs_),
            GameSeat.game_id.in_(terminal_ids),
        )
        for ab_id, faction in (await session.execute(stmt)).all():
            bucket = counters[ab_id]
            bucket["total"] += 1
            if faction == Faction.MAFIA.value:
                bucket["mafia"] += 1
            elif faction == Faction.TOWN.value:
                bucket["town"] += 1
    return {
        ab_id: AgentBuildProvisional(
            agent_build_id=ab_id,
            total_games=c["total"],
            town_games=c["town"],
            mafia_games=c["mafia"],
            provisional=_is_provisional(c["total"], c["town"], c["mafia"]),
        )
        for ab_id, c in counters.items()
    }


async def _model_seat_counts(
    session: AsyncSession,
    game_ids: Iterable[uuid.UUID],
) -> list[ModelSeatCounts]:
    ids = list(game_ids)
    if not ids:
        return []
    stmt = select(GameSeat.agent_build_id, GameSeat.faction).where(GameSeat.game_id.in_(ids))
    counters: dict[uuid.UUID, dict[str, int]] = {}
    for agent_build_id, faction in (await session.execute(stmt)).all():
        if agent_build_id is None:
            continue
        bucket = counters.setdefault(
            agent_build_id,
            {Faction.TOWN.value: 0, Faction.MAFIA.value: 0},
        )
        if faction in bucket:
            bucket[str(faction)] += 1
    return [
        ModelSeatCounts(
            agent_build_id=agent_build_id,
            town_seats=counts[Faction.TOWN.value],
            mafia_seats=counts[Faction.MAFIA.value],
            total_seats=counts[Faction.TOWN.value] + counts[Faction.MAFIA.value],
        )
        for agent_build_id, counts in counters.items()
        if counts[Faction.TOWN.value] + counts[Faction.MAFIA.value] > 0
    ]


def _faction_totals_for_ruleset(ruleset_id: str) -> tuple[int, int]:
    ruleset = get_ruleset(ruleset_id)
    town = 0
    mafia = 0
    for role, count in ruleset.ROLE_COUNTS.items():
        faction = ruleset.faction_for(role)
        if faction is Faction.TOWN:
            town += count
        elif faction is Faction.MAFIA:
            mafia += count
    return town, mafia


def _seat_counts_within_tolerance(
    counts: Iterable[ModelSeatCounts],
    *,
    town_per_game: int,
    mafia_per_game: int,
    player_count: int,
    tolerance_seats: int,
) -> bool:
    tolerance_numerator = tolerance_seats * player_count
    for entry in counts:
        town_diff = abs(entry.town_seats * player_count - entry.total_seats * town_per_game)
        mafia_diff = abs(entry.mafia_seats * player_count - entry.total_seats * mafia_per_game)
        if town_diff > tolerance_numerator or mafia_diff > tolerance_numerator:
            return False
    return True


async def _balance_gate_satisfied(
    session: AsyncSession,
    *,
    gauntlet: Gauntlet,
    child_ids: Iterable[uuid.UUID],
    balance_tolerance_seats: int,
) -> bool:
    statuses = await _child_game_statuses(session, child_ids)
    if any(status == _FAILED_STATUS for status in statuses.values()):
        return True
    completed_ids = [game_id for game_id, status in statuses.items() if status == _COMPLETED_STATUS]
    if not completed_ids:
        return True
    seat_counts = await _model_seat_counts(session, completed_ids)
    if not seat_counts:
        return True
    town_per_game, mafia_per_game = _faction_totals_for_ruleset(gauntlet.ruleset_id)
    player_count = town_per_game + mafia_per_game
    if player_count <= 0:
        return True
    return _seat_counts_within_tolerance(
        seat_counts,
        town_per_game=town_per_game,
        mafia_per_game=mafia_per_game,
        player_count=player_count,
        tolerance_seats=balance_tolerance_seats,
    )


async def diagnostics_for_games(
    session: AsyncSession,
    game_ids: list[uuid.UUID],
) -> GauntletDiagnostics:
    games_completed = len(game_ids)
    if not game_ids:
        return GauntletDiagnostics(
            games_completed=0,
            timeout_rate=0.0,
            invalid_action_rate=0.0,
            average_public_message_chars=0.0,
        )

    counts_stmt = (
        select(GameEvent.event_type, func.count())
        .where(
            GameEvent.game_id.in_(game_ids),
            GameEvent.event_type.in_(SUBMISSION_EVENT_TYPES),
        )
        .group_by(GameEvent.event_type)
    )
    counts: dict[str, int] = {
        row[0]: int(row[1]) for row in (await session.execute(counts_stmt)).all()
    }
    total_attempts = sum(counts.values())
    timeout_count = counts.get(TIMEOUT_EVENT_TYPE, 0)
    invalid_count = counts.get(INVALID_EVENT_TYPE, 0)
    timeout_rate = (timeout_count / total_attempts) if total_attempts else 0.0
    invalid_rate = (invalid_count / total_attempts) if total_attempts else 0.0

    pm_stmt = select(GameEvent.payload).where(
        GameEvent.game_id.in_(game_ids),
        GameEvent.event_type == PUBLIC_MESSAGE_EVENT_TYPE,
    )
    pm_payloads = list((await session.execute(pm_stmt)).scalars().all())
    pm_total_chars = 0
    pm_count = 0
    for payload in pm_payloads:
        text = payload.get("text") if isinstance(payload, dict) else None
        if isinstance(text, str):
            pm_total_chars += len(text)
            pm_count += 1
    avg_chars = (pm_total_chars / pm_count) if pm_count else 0.0

    return GauntletDiagnostics(
        games_completed=games_completed,
        timeout_rate=timeout_rate,
        invalid_action_rate=invalid_rate,
        average_public_message_chars=avg_chars,
    )


async def finalize_gauntlet_if_done(
    session: AsyncSession,
    gauntlet_id: uuid.UUID,
    *,
    balance_tolerance_seats: int = DEFAULT_BALANCE_TOLERANCE_SEATS,
    completed_at: datetime | None = None,
) -> GauntletFinalized | None:
    """Mark a gauntlet ``COMPLETED`` and return diagnostics, or return ``None``.

    Returns ``None`` when the gauntlet does not exist, is already in
    ``COMPLETED`` status, has no child games, has at least one child game
    whose row status is still non-terminal, or fails the balance gate. The
    first call after every child game has terminalized does the status flip and
    returns the finalized payload; subsequent calls are no-ops that return
    ``None``.
    """
    if balance_tolerance_seats < 0:
        raise ValueError("balance_tolerance_seats must be >= 0")

    gauntlet = await session.get(Gauntlet, gauntlet_id)
    if gauntlet is None or gauntlet.status == "COMPLETED":
        return None

    games_stmt = select(Game.id).where(Game.gauntlet_id == gauntlet_id)
    child_ids = list((await session.execute(games_stmt)).scalars().all())
    if not child_ids:
        return None
    if not await _all_games_terminal(session, child_ids):
        return None
    if not await _balance_gate_satisfied(
        session,
        gauntlet=gauntlet,
        child_ids=child_ids,
        balance_tolerance_seats=balance_tolerance_seats,
    ):
        return None

    completed_ids = await _completed_game_ids(session, child_ids)

    seats_stmt = (
        select(GameSeat.agent_build_id).where(GameSeat.game_id.in_(completed_ids)).distinct()
    )
    # ``agent_build_id`` is nullable since Wave 9 (human seats). Gauntlet games
    # are AI-only, but filter defensively so the rating path never sees a None.
    agent_build_ids = [
        ab_id for ab_id in (await session.execute(seats_stmt)).scalars().all() if ab_id is not None
    ]

    provisional_by_ab = await _provisional_for_agent_builds(
        session,
        league_id=gauntlet.league_id,
        agent_build_ids=agent_build_ids,
    )
    diagnostics = await diagnostics_for_games(session, completed_ids)

    finalized_at = completed_at if completed_at is not None else datetime.now(UTC)
    gauntlet.status = "COMPLETED"
    gauntlet.completed_at = finalized_at
    gauntlet.heartbeat_at = None
    await session.flush()
    await session.commit()

    return GauntletFinalized(
        gauntlet_id=gauntlet_id,
        status="COMPLETED",
        completed_at=finalized_at,
        provisional_by_agent_build=provisional_by_ab,
        diagnostics=diagnostics,
    )


__all__ = [
    "DEFAULT_BALANCE_TOLERANCE_SEATS",
    "PROVISIONAL_MAFIA_GAMES",
    "PROVISIONAL_TOTAL_GAMES",
    "PROVISIONAL_TOWN_GAMES",
    "AgentBuildProvisional",
    "GauntletDiagnostics",
    "GauntletFinalized",
    "TerminalProgress",
    "diagnostics_for_games",
    "finalize_gauntlet_if_done",
    "gauntlet_child_progress",
]
