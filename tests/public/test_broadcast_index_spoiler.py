"""Spoiler-safety tests for the broadcast index (US-087).

Asserts that:
- A LIVE game's public entry omits terminal_result / outcome fields regardless
  of what the underlying DB row contains.
- A RECENT game's public entry exposes terminal_result.
- HIDDEN games are invisible on both live and recent surfaces.
- mark_* state transitions work correctly.
- Querying for a non-existent game_id returns None from mark_* helpers.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import Game
from padrino.public.broadcast_index import (
    BroadcastState,
    list_live,
    list_recent,
    mark_hidden,
    mark_live,
    mark_recent,
)

_OUTCOME = {"winner": "MAFIA", "cause": "PARITY"}
_TOWN_OUTCOME = {"winner": "TOWN", "cause": "VOTE"}


async def _make_game(
    session: AsyncSession,
    *,
    terminal_result: dict[str, Any] | None = None,
    status: str = "RUNNING",
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed="test-seed-087",
        status=status,
        terminal_result=terminal_result,
    )
    session.add(g)
    await session.flush()
    return g


# ---------------------------------------------------------------------------
# Spoiler-safety: LIVE games must not leak outcome
# ---------------------------------------------------------------------------


async def test_live_game_omits_terminal_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session, terminal_result=_OUTCOME, status="COMPLETED")
        await mark_live(session, game.id)

    async with session_factory() as session:
        entries = await list_live(session)

    assert len(entries) == 1
    assert entries[0].terminal_result is None, (
        "LIVE game must not expose terminal_result — spoiler protection violated"
    )
    assert entries[0].broadcast_state == BroadcastState.LIVE


async def test_live_game_payload_has_no_outcome_keys(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session, terminal_result=_OUTCOME, status="COMPLETED")
        await mark_live(session, game.id)

    async with session_factory() as session:
        entries = await list_live(session)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.terminal_result is None
    assert entry.broadcast_state == BroadcastState.LIVE
    assert entry.game_id == game.id


# ---------------------------------------------------------------------------
# RECENT games expose outcome
# ---------------------------------------------------------------------------


async def test_recent_game_exposes_terminal_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session, terminal_result=_TOWN_OUTCOME, status="COMPLETED")
        await mark_recent(session, game.id)

    async with session_factory() as session:
        entries = await list_recent(session)

    assert len(entries) == 1
    assert entries[0].terminal_result == _TOWN_OUTCOME, (
        "RECENT game must expose terminal_result to viewers"
    )
    assert entries[0].broadcast_state == BroadcastState.RECENT


# ---------------------------------------------------------------------------
# Partition: LIVE vs RECENT vs HIDDEN are mutually exclusive on query surfaces
# ---------------------------------------------------------------------------


async def test_live_game_absent_from_list_recent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        await mark_live(session, game.id)

    async with session_factory() as session:
        live_ids = {e.game_id for e in await list_live(session)}
        recent_ids = {e.game_id for e in await list_recent(session)}

    assert game.id in live_ids
    assert game.id not in recent_ids


async def test_recent_game_absent_from_list_live(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session, terminal_result=_OUTCOME, status="COMPLETED")
        await mark_recent(session, game.id)

    async with session_factory() as session:
        live_ids = {e.game_id for e in await list_live(session)}
        recent_ids = {e.game_id for e in await list_recent(session)}

    assert game.id not in live_ids
    assert game.id in recent_ids


async def test_hidden_game_absent_from_both_surfaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        # default broadcast_state is HIDDEN — no mark_* call needed

    async with session_factory() as session:
        live_ids = {e.game_id for e in await list_live(session)}
        recent_ids = {e.game_id for e in await list_recent(session)}

    assert game.id not in live_ids
    assert game.id not in recent_ids


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def test_mark_transitions_cycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        assert game.broadcast_state == BroadcastState.HIDDEN.value

        returned = await mark_live(session, game.id)
        assert returned is not None
        assert returned.broadcast_state == BroadcastState.LIVE.value

        returned = await mark_recent(session, game.id)
        assert returned is not None
        assert returned.broadcast_state == BroadcastState.RECENT.value

        returned = await mark_hidden(session, game.id)
        assert returned is not None
        assert returned.broadcast_state == BroadcastState.HIDDEN.value


async def test_mark_live_unknown_game_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        result = await mark_live(session, uuid.uuid4())
    assert result is None


async def test_mark_recent_unknown_game_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        result = await mark_recent(session, uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# list_recent respects limit
# ---------------------------------------------------------------------------


async def test_list_recent_respects_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        for _ in range(5):
            game = await _make_game(session, terminal_result=_OUTCOME, status="COMPLETED")
            await mark_recent(session, game.id)

    async with session_factory() as session:
        entries = await list_recent(session, limit=3)

    assert len(entries) == 3


async def test_default_broadcast_state_is_hidden(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        assert game.broadcast_state == BroadcastState.HIDDEN.value
