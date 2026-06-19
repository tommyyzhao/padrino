"""Tests for per-human play-history stats from the event log (US-145).

Covers the pure participant-keyed extractor
(:func:`compute_participant_stats`) and the DB-backed producer
(:func:`refresh_human_player_stats_for_game`), plus the segregation guarantee
that scientific-league (AI-only) games write ZERO ``human_player_stats`` rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.analytics.deterministic import compute_participant_stats
from padrino.analytics.human_stats import (
    refresh_human_player_stats,
    refresh_human_player_stats_for_game,
)
from padrino.db.models import Game, GameEvent, GameSeat, HumanPlayerStats, Principal

_RULESET = "mini7_v1"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

_ROLE_ASSIGNMENTS: list[dict[str, str]] = [
    {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P03", "role": "DETECTIVE", "faction": "TOWN"},
    {"public_player_id": "P04", "role": "DOCTOR", "faction": "TOWN"},
    {"public_player_id": "P05", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P06", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN"},
]


def _ev(
    seq: int,
    event_type: str,
    phase: str,
    actor: str | None,
    payload: dict[str, Any],
    visibility: str = "PUBLIC",
) -> dict[str, Any]:
    return {
        "sequence": seq,
        "event_type": event_type,
        "phase": phase,
        "visibility": visibility,
        "actor_player_id": actor,
        "payload": payload,
    }


# TOWN wins. P03 (DETECTIVE) investigates P01 (MAFIA -> accurate) then P04 (TOWN
# -> inaccurate). P03 votes P01 (MAFIA, accurate) day 1, then abstains day 2.
# P07 (VILLAGER) is night-killed (does not survive). P01/P02 eliminated.
_TOWN_WIN_GAME: list[dict[str, Any]] = [
    _ev(1, "RolesAssigned", "SETUP", None, {"assignments": _ROLE_ASSIGNMENTS}, "SYSTEM"),
    _ev(2, "VoteSubmitted", "DAY_1_VOTE", "P03", {"target": "P01", "is_abstain": False}),
    _ev(3, "VoteSubmitted", "DAY_1_VOTE", "P05", {"target": "P02", "is_abstain": False}),
    _ev(
        4,
        "PlayerEliminated",
        "DAY_1_VOTE",
        None,
        {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA", "cause": "DAY_VOTE"},
    ),
    _ev(
        5,
        "DetectiveResultDelivered",
        "NIGHT_1_ACTIONS",
        "P03",
        {"target": "P01", "finding": "MAFIA"},
        "PRIVATE",
    ),
    _ev(
        6,
        "PlayerEliminated",
        "NIGHT_1_ACTIONS",
        None,
        {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN", "cause": "NIGHT_KILL"},
    ),
    _ev(7, "VoteSubmitted", "DAY_2_VOTE", "P03", {"target": None, "is_abstain": True}),
    _ev(
        8,
        "DetectiveResultDelivered",
        "NIGHT_2_ACTIONS",
        "P03",
        {"target": "P04", "finding": "TOWN"},
        "PRIVATE",
    ),
    _ev(
        9,
        "PlayerEliminated",
        "DAY_2_VOTE",
        None,
        {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA", "cause": "DAY_VOTE"},
    ),
    _ev(10, "GameTerminated", "DAY_2_VOTE", None, {"winner": "TOWN", "reason": "all_mafia_dead"}),
]


# ---------------------------------------------------------------------------
# Pure extractor
# ---------------------------------------------------------------------------


def test_participant_stats_detective_winner() -> None:
    stats = compute_participant_stats(_TOWN_WIN_GAME, {"P03": "principal-a"})
    assert len(stats) == 1
    s = stats[0]
    assert s.public_player_id == "P03"
    assert (s.role, s.faction) == ("DETECTIVE", "TOWN")
    assert (s.won, s.drew, s.lost) == (1, 0, 0)
    assert s.survived == 1
    # One non-abstain day vote, on a mafia seat; the abstain is not counted.
    assert (s.voting_total, s.voting_accurate) == (1, 1)
    assert s.voting_accuracy == 1.0
    # Two investigations, one returned MAFIA.
    assert (s.detection_total, s.detection_accurate) == (2, 1)
    assert s.detection_accuracy == 0.5


def test_participant_stats_losing_mafia_and_killed_villager() -> None:
    stats = compute_participant_stats(_TOWN_WIN_GAME, {"P01": "pa", "P07": "pb"})
    by_pid = {s.public_player_id: s for s in stats}
    mafia = by_pid["P01"]
    assert (mafia.won, mafia.drew, mafia.lost) == (0, 0, 1)
    assert mafia.faction == "MAFIA"
    villager = by_pid["P07"]
    assert villager.survived == 0  # night-killed
    assert (villager.won, villager.drew, villager.lost) == (1, 0, 0)  # TOWN won


def test_participant_stats_skips_unmapped_and_unknown_seats() -> None:
    # P99 is not in the role map; it is skipped rather than crashing.
    stats = compute_participant_stats(_TOWN_WIN_GAME, {"P99": "ghost"})
    assert stats == ()


def test_participant_stats_draw_is_neither_win_nor_loss() -> None:
    draw_game = [
        _ev(1, "RolesAssigned", "SETUP", None, {"assignments": _ROLE_ASSIGNMENTS}, "SYSTEM"),
        _ev(2, "GameTerminated", "DAY_5_VOTE", None, {"winner": "DRAW", "reason": "max_days"}),
    ]
    stats = compute_participant_stats(draw_game, {"P03": "pa"})
    s = stats[0]
    assert (s.won, s.drew, s.lost) == (0, 1, 0)


def test_participant_stats_is_deterministic() -> None:
    a = compute_participant_stats(_TOWN_WIN_GAME, {"P03": "pa", "P05": "pb"})
    b = compute_participant_stats(_TOWN_WIN_GAME, {"P05": "pb", "P03": "pa"})
    assert a == b
    assert [s.public_player_id for s in a] == ["P03", "P05"]  # sorted by pid


# ---------------------------------------------------------------------------
# DB producer
# ---------------------------------------------------------------------------


def _gevent(game_id: uuid.UUID, e: dict[str, Any]) -> GameEvent:
    return GameEvent(
        game_id=game_id,
        sequence=e["sequence"],
        event_type=e["event_type"],
        phase=e["phase"],
        visibility=e["visibility"],
        actor_player_id=e["actor_player_id"],
        payload=e["payload"],
        prev_event_hash="0" * 64,
        event_hash=f"{e['sequence']:064x}",
    )


async def _make_principal(session: AsyncSession) -> uuid.UUID:
    p = Principal(kind="guest", display_name=f"human-{uuid.uuid4()}")
    session.add(p)
    await session.flush()
    return p.id


async def _seed_human_game(
    session: AsyncSession,
    *,
    detective_principal: uuid.UUID,
    events: list[dict[str, Any]],
    status: str = "COMPLETED",
) -> Game:
    """A COMPLETED human-lane game where P03 (DETECTIVE) is human-occupied."""
    g = Game(ruleset_id=_RULESET, game_seed=f"hg-{uuid.uuid4()}", status=status)
    session.add(g)
    await session.flush()
    for a in _ROLE_ASSIGNMENTS:
        is_human = a["public_player_id"] == "P03"
        session.add(
            GameSeat(
                game_id=g.id,
                public_player_id=a["public_player_id"],
                seat_index=int(a["public_player_id"][1:]),
                agent_build_id=None,
                seat_kind="HUMAN" if is_human else "AI",
                occupant_principal_id=detective_principal if is_human else None,
                role=a["role"],
                faction=a["faction"],
                alive=a["public_player_id"] not in {"P01", "P02", "P07"},
            )
        )
    for e in events:
        session.add(_gevent(g.id, e))
    await session.flush()
    return g


@pytest.mark.asyncio
async def test_refresh_for_game_writes_human_stats_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = await _make_principal(session)
        game = await _seed_human_game(session, detective_principal=principal, events=_TOWN_WIN_GAME)
        refreshed = await refresh_human_player_stats_for_game(session, game.id, now=_NOW)
        assert len(refreshed) == 1

    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanPlayerStats).where(HumanPlayerStats.principal_id == principal)
            )
        ).scalar_one()
        assert row.ruleset_id == _RULESET
        assert row.games == 1
        assert (row.wins, row.draws, row.losses) == (1, 0, 0)
        assert row.survived_games == 1
        assert (row.voting_total_votes, row.voting_accurate_votes) == (1, 1)
        assert (row.detection_total, row.detection_accurate) == (2, 1)
        roles = {r["name"]: r for r in json.loads(row.role_win_rates_json)}
        assert roles["DETECTIVE"] == {"name": "DETECTIVE", "wins": 1, "games": 1}
        factions = {r["name"]: r for r in json.loads(row.faction_win_rates_json)}
        assert factions["TOWN"] == {"name": "TOWN", "wins": 1, "games": 1}


@pytest.mark.asyncio
async def test_refresh_rolls_up_across_games_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = await _make_principal(session)
        g1 = await _seed_human_game(session, detective_principal=principal, events=_TOWN_WIN_GAME)
        g2 = await _seed_human_game(session, detective_principal=principal, events=_TOWN_WIN_GAME)
        await refresh_human_player_stats_for_game(session, g1.id, now=_NOW)
        await refresh_human_player_stats_for_game(session, g2.id, now=_NOW)
        # Re-running must update in place under the unique constraint, not insert.
        await refresh_human_player_stats_for_game(session, g2.id, now=_NOW)

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HumanPlayerStats).where(HumanPlayerStats.principal_id == principal)
                )
            ).scalars()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.games == 2
        assert row.wins == 2
        assert row.detection_total == 4
        assert row.detection_accurate == 2


@pytest.mark.asyncio
async def test_scientific_ai_only_game_writes_zero_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        g = Game(ruleset_id=_RULESET, game_seed=f"sci-{uuid.uuid4()}", status="COMPLETED")
        session.add(g)
        await session.flush()
        for a in _ROLE_ASSIGNMENTS:
            session.add(
                GameSeat(
                    game_id=g.id,
                    public_player_id=a["public_player_id"],
                    seat_index=int(a["public_player_id"][1:]),
                    agent_build_id=None,
                    seat_kind="AI",
                    occupant_principal_id=None,
                    role=a["role"],
                    faction=a["faction"],
                    alive=True,
                )
            )
        for e in _TOWN_WIN_GAME:
            session.add(_gevent(g.id, e))
        await session.flush()
        refreshed = await refresh_human_player_stats_for_game(session, g.id, now=_NOW)
        assert refreshed == []

    async with session_factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(HumanPlayerStats))
        ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_refresh_skips_unknown_principal_and_incomplete_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        # Principal with no completed human games -> no row.
        assert (
            await refresh_human_player_stats(
                session, principal_id=uuid.uuid4(), ruleset_id=_RULESET, now=_NOW
            )
            is None
        )
        # A RUNNING human game is a no-op for refresh-for-game.
        principal = await _make_principal(session)
        g = await _seed_human_game(
            session, detective_principal=principal, events=_TOWN_WIN_GAME, status="RUNNING"
        )
        assert await refresh_human_player_stats_for_game(session, g.id, now=_NOW) == []
