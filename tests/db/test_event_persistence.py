"""US-032: end-to-end persistence test for game_events + llm_calls.

Runs a short scripted game with a real aiosqlite DB attached as the runner's
``GamePersistence`` target, then asserts the persisted event chain matches the
in-memory ``EventLog`` byte-for-byte (sequence, prev_event_hash, event_hash,
event_type, payload). Also verifies that one llm_call row is recorded for
every adapter call observed in ``GameOutcome.llm_calls``.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import (
    make_mafia_win_script,
    make_town_win_script,
    mini7_phase_ids,  # noqa: F401  # used by other test files; keep export stable
)

_GAME_SEED = "seed-persistence-001"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _seed_game_row(
    session_factory: async_sessionmaker[AsyncSession],
    game_seed: str,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=game_seed,
            status="RUNNING",
        )
        game_id = game.id
    return game_id


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def test_persisted_chain_matches_event_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    adapter = DeterministicMockAdapter(script)
    game_db_id = await _seed_game_row(session_factory, _GAME_SEED)

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_db_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-PERSIST", game_seed=_GAME_SEED, timeout_s=1.0),
        adapter,
        ranked=False,
        persistence=persistence,
    )

    in_memory = outcome.event_log.events

    async with session_factory() as session:
        persisted = await events_repo.list_events(session, game_db_id)

    assert len(persisted) == len(in_memory), (
        f"persisted row count ({len(persisted)}) != in-memory ({len(in_memory)})"
    )

    for stored, row in zip(in_memory, persisted, strict=True):
        assert row.sequence == stored.sequence
        assert row.prev_event_hash == stored.prev_event_hash
        assert row.event_hash == stored.event_hash
        assert row.event_type == stored.body["event_type"]
        assert row.phase == stored.body["phase"]
        assert row.visibility == stored.body["visibility"]
        assert row.actor_player_id == stored.body.get("actor_player_id")
        assert row.payload == stored.body.get("payload", {})


async def test_visibility_filter_returns_public_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    adapter = DeterministicMockAdapter(script)
    game_db_id = await _seed_game_row(session_factory, _GAME_SEED)
    persistence = GamePersistence(session_factory=session_factory, game_id=game_db_id)

    await run_game(
        GameConfig(game_id="G-PERSIST-VIS", game_seed=_GAME_SEED, timeout_s=1.0),
        adapter,
        ranked=False,
        persistence=persistence,
    )

    async with session_factory() as session:
        public_rows = await events_repo.list_events(session, game_db_id, visibility_filter="PUBLIC")
        private_rows = await events_repo.list_events(
            session, game_db_id, visibility_filter="PRIVATE"
        )
        system_rows = await events_repo.list_events(session, game_db_id, visibility_filter="SYSTEM")

    assert all(row.visibility == "PUBLIC" for row in public_rows)
    assert all(row.visibility == "PRIVATE" for row in private_rows)
    assert all(row.visibility == "SYSTEM" for row in system_rows)
    # At least one GameTerminated (PUBLIC), one PrivateMessageSubmitted or
    # MafiaKillVoteSubmitted (PRIVATE), and several SYSTEM phase markers.
    assert any(r.event_type == "GameTerminated" for r in public_rows)
    assert any(r.event_type == "PhaseStarted" for r in system_rows)


async def test_llm_calls_persisted_per_adapter_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, _, _ = _split_factions()
    script = make_mafia_win_script(mafia_ids=mafia, town_ids=town)
    adapter = DeterministicMockAdapter(script)
    game_db_id = await _seed_game_row(session_factory, _GAME_SEED)
    persistence = GamePersistence(session_factory=session_factory, game_id=game_db_id)

    outcome = await run_game(
        GameConfig(game_id="G-PERSIST-LLM", game_seed=_GAME_SEED, timeout_s=1.0),
        adapter,
        ranked=False,
        persistence=persistence,
    )

    async with session_factory() as session:
        rows = await llm_calls_repo.list_for_game(session, game_db_id)

    assert len(rows) == len(outcome.llm_calls)
    assert len(rows) > 0
    for row in rows:
        assert row.game_id == game_db_id
        assert row.public_player_id.startswith("P")
        assert row.phase  # non-empty string
        assert row.request_prompt_hash  # non-empty hash
        assert row.status == "ok"
        assert row.agent_build_id is None  # no build mapping passed
        # parsed_response is the JSON dump of AgentResponse
        assert isinstance(row.parsed_response, dict)
        assert "action" in row.parsed_response


async def test_run_without_persistence_does_not_touch_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    adapter = DeterministicMockAdapter(script)
    game_db_id = await _seed_game_row(session_factory, _GAME_SEED)

    await run_game(
        GameConfig(game_id="G-NO-PERSIST", game_seed=_GAME_SEED, timeout_s=1.0),
        adapter,
        ranked=False,
    )

    async with session_factory() as session:
        events = await events_repo.list_events(session, game_db_id)
        llms = await llm_calls_repo.list_for_game(session, game_db_id)

    assert events == []
    assert llms == []


def test_repositories_do_not_import_forbidden_modules() -> None:
    """AST guard: repository modules stay free of random/secrets/time/clock imports."""
    forbidden = {"random", "secrets", "time"}
    for relpath in (
        "src/padrino/db/repositories/events.py",
        "src/padrino/db/repositories/llm_calls.py",
    ):
        source = Path(relpath).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, (
                        f"{relpath} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, f"{relpath} imports from {node.module}"
