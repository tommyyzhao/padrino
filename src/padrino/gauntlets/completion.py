"""Gauntlet completion + leaderboard provisional-flag logic.

Once every child game of a gauntlet has emitted a ``GameTerminated`` event,
:func:`finalize_gauntlet_if_done` flips the gauntlet status to ``COMPLETED``,
stamps ``completed_at``, and returns aggregate diagnostics plus the
provisional flag for each agent build that played in the gauntlet.

Provisional thresholds (per ``prd.md`` §10):

* ``total_games >= 30``
* ``mafia_games >= 5``
* ``town_games >= 15``

Counts are league-scoped: per-agent_build totals span every terminal game
under the gauntlet's league, not just the gauntlet's own clones, so the
leaderboard view of "enough games played" is consistent across gauntlets.

Aggregate diagnostics are scoped to the gauntlet's child games only.

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
from padrino.db.models import Game, GameEvent, GameSeat, Gauntlet

PROVISIONAL_TOTAL_GAMES: Final[int] = 30
PROVISIONAL_MAFIA_GAMES: Final[int] = 5
PROVISIONAL_TOWN_GAMES: Final[int] = 15

_COMPLETED_STATUS: Final[str] = "COMPLETED"
_PUBLIC_MESSAGE_EVENT_TYPE: Final[str] = "PublicMessageSubmitted"
_TIMEOUT_EVENT_TYPE: Final[str] = "ActionTimedOut"
_INVALID_EVENT_TYPE: Final[str] = "OutputInvalid"

# Submission events that count toward the rate denominator. Failure events
# are included so the denominator is "total turn-attempts" rather than only
# successful submissions; that keeps timeout / invalid percentages bounded
# in [0, 1] even when most turns failed.
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
    stmt = select(Game.id).where(
        Game.id.in_(ids),
        Game.status == _COMPLETED_STATUS,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return set(rows) == set(ids)


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
            GameEvent.event_type.in_(_SUBMISSION_EVENT_TYPES),
        )
        .group_by(GameEvent.event_type)
    )
    counts: dict[str, int] = {
        row[0]: int(row[1]) for row in (await session.execute(counts_stmt)).all()
    }
    total_attempts = sum(counts.values())
    timeout_count = counts.get(_TIMEOUT_EVENT_TYPE, 0)
    invalid_count = counts.get(_INVALID_EVENT_TYPE, 0)
    timeout_rate = (timeout_count / total_attempts) if total_attempts else 0.0
    invalid_rate = (invalid_count / total_attempts) if total_attempts else 0.0

    pm_stmt = select(GameEvent.payload).where(
        GameEvent.game_id.in_(game_ids),
        GameEvent.event_type == _PUBLIC_MESSAGE_EVENT_TYPE,
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
) -> GauntletFinalized | None:
    """Mark a gauntlet ``COMPLETED`` and return diagnostics, or return ``None``.

    Returns ``None`` when the gauntlet does not exist, is already in
    ``COMPLETED`` status, has no child games, or has at least one child game
    that has not emitted a ``GameTerminated`` event yet. The first call after
    every child game has terminated does the status flip and returns the
    finalized payload; subsequent calls are no-ops that return ``None``.
    """
    gauntlet = await session.get(Gauntlet, gauntlet_id)
    if gauntlet is None or gauntlet.status == "COMPLETED":
        return None

    games_stmt = select(Game.id).where(Game.gauntlet_id == gauntlet_id)
    child_ids = list((await session.execute(games_stmt)).scalars().all())
    if not child_ids:
        return None
    if not await _all_games_terminal(session, child_ids):
        return None

    seats_stmt = select(GameSeat.agent_build_id).where(GameSeat.game_id.in_(child_ids)).distinct()
    agent_build_ids = list((await session.execute(seats_stmt)).scalars().all())

    provisional_by_ab = await _provisional_for_agent_builds(
        session,
        league_id=gauntlet.league_id,
        agent_build_ids=agent_build_ids,
    )
    diagnostics = await diagnostics_for_games(session, child_ids)

    completed_at = datetime.now(UTC)
    gauntlet.status = "COMPLETED"
    gauntlet.completed_at = completed_at
    await session.flush()
    await session.commit()

    return GauntletFinalized(
        gauntlet_id=gauntlet_id,
        status="COMPLETED",
        completed_at=completed_at,
        provisional_by_agent_build=provisional_by_ab,
        diagnostics=diagnostics,
    )


__all__ = [
    "PROVISIONAL_MAFIA_GAMES",
    "PROVISIONAL_TOTAL_GAMES",
    "PROVISIONAL_TOWN_GAMES",
    "AgentBuildProvisional",
    "GauntletDiagnostics",
    "GauntletFinalized",
    "diagnostics_for_games",
    "finalize_gauntlet_if_done",
]
