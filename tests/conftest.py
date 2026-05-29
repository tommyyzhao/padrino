"""Shared pytest fixtures and scripted-agent helpers.

The helpers below assemble ``dict[tuple[str, str], AgentResponse]`` scripts
keyed by ``(phase_id, public_player_id)`` — the shape consumed by
:class:`padrino.llm.mock.DeterministicMockAdapter`. Integration tests
(US-027+) compose these to drive complete games without a real LLM.

This module also installs a ``pytest_collection_modifyitems`` hook that
deselects the ``live_llm`` marker by default. The recorded-cassette contract
suite under ``tests/llm/test_litellm_contract.py`` (US-051) opts in via
``-m live_llm`` or the ``--live-llm`` flag. Likewise, ``postgres``-marked
tests (US-057) are skipped unless a Docker daemon is reachable so contributors
without docker can still run the default ``pytest`` invocation.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.enums import ActionType
from padrino.core.rulesets import mini7_v1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="run recorded-cassette live LLM contract tests (US-051)",
    )
    parser.addoption(
        "--postgres",
        action="store_true",
        default=False,
        help="run unit/integration tests against real PostgreSQL instead of SQLite in-memory",
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Default-skip the ``live_llm``, ``postgres`` and ``docker`` markers.

    ``live_llm`` (US-051) is opt-in via ``--live-llm`` or ``-m live_llm``
    (recorded cassettes). ``postgres`` (US-057) requires a Docker daemon — it
    auto-skips when one isn't reachable, but runs by default on docker-equipped
    machines because the per-test cost is small (one shared container).
    ``docker`` (US-064) brings up the full compose stack including image
    builds, which is expensive — so it is strictly opt-in via ``-m docker``
    even when a Docker daemon is available.
    """

    markexpr = (config.option.markexpr or "").strip()

    skip_live = pytest.mark.skip(
        reason="live_llm cassette tests are opt-in; pass --live-llm or '-m live_llm'"
    )
    skip_postgres = pytest.mark.skip(reason="postgres tests require a reachable Docker daemon")
    skip_docker_unavailable = pytest.mark.skip(
        reason="docker tests require a reachable Docker daemon"
    )
    skip_docker_optin = pytest.mark.skip(reason="docker tests are opt-in; pass '-m docker'")
    live_opted_in = config.getoption("--live-llm") or (
        "live_llm" in markexpr and "not live_llm" not in markexpr
    )
    docker_opted_in = "docker" in markexpr and "not docker" not in markexpr
    docker_available = _docker_available()

    for item in items:
        if "live_llm" in item.keywords and not live_opted_in:
            item.add_marker(skip_live)
        if "postgres" in item.keywords and not docker_available:
            item.add_marker(skip_postgres)
        if "docker" in item.keywords:
            if not docker_available:
                item.add_marker(skip_docker_unavailable)
            elif not docker_opted_in:
                item.add_marker(skip_docker_optin)


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _phase_default(phase_id: str) -> AgentResponse:
    if phase_id.endswith("_VOTE"):
        return _response(ActionType.ABSTAIN, None)
    return _response(ActionType.NOOP, None)


def mini7_phase_ids() -> tuple[str, ...]:
    """All phase ids mini7_v1 may emit, in chronological order.

    Excludes ``SETUP`` and ``TERMINAL`` since those phases never prompt seats.
    """
    out: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for d in range(1, mini7_v1.MAX_DAYS + 1):
        for r in range(1, mini7_v1.DISCUSSION_ROUNDS_PER_DAY + 1):
            out.append(f"DAY_{d}_DISCUSSION_ROUND_{r}")
        out.append(f"DAY_{d}_VOTE")
        out.append(f"NIGHT_{d}_MAFIA_DISCUSSION")
        out.append(f"NIGHT_{d}_ACTIONS")
    return tuple(out)


def make_villager_script(
    seat_ids: Sequence[str],
    phase_ids: Sequence[str],
    *,
    votes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[tuple[str, str], AgentResponse]:
    """Build a NOOP/ABSTAIN baseline for ``seat_ids`` across ``phase_ids``.

    ``votes[phase_id][seat_id] = target`` upgrades that single seat's vote-phase
    response from ABSTAIN to ``VOTE(target)``.
    """
    overrides: Mapping[str, Mapping[str, str]] = votes or {}
    script: dict[tuple[str, str], AgentResponse] = {}
    for phase_id in phase_ids:
        phase_overrides = overrides.get(phase_id, {})
        for sid in seat_ids:
            if sid in phase_overrides:
                script[(phase_id, sid)] = _response(ActionType.VOTE, phase_overrides[sid])
            else:
                script[(phase_id, sid)] = _phase_default(phase_id)
    return script


def make_mafia_script(
    mafia_ids: Sequence[str],
    phase_ids: Sequence[str],
    *,
    night_kill_targets: Mapping[str, str] | None = None,
    votes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[tuple[str, str], AgentResponse]:
    """Build a mafia-side script.

    ``night_kill_targets[phase_id] = target`` makes every mafia seat submit
    ``MAFIA_KILL(target)`` on that night-actions phase. Outside listed kill
    phases each mafia seat defaults to NOOP (or ABSTAIN on day votes). The
    ``votes`` override mirrors :func:`make_villager_script`.
    """
    kills: Mapping[str, str] = night_kill_targets or {}
    vote_overrides: Mapping[str, Mapping[str, str]] = votes or {}
    script: dict[tuple[str, str], AgentResponse] = {}
    for phase_id in phase_ids:
        phase_votes = vote_overrides.get(phase_id, {})
        for sid in mafia_ids:
            if sid in phase_votes:
                script[(phase_id, sid)] = _response(ActionType.VOTE, phase_votes[sid])
            elif phase_id in kills and phase_id.endswith("_ACTIONS"):
                script[(phase_id, sid)] = _response(ActionType.MAFIA_KILL, kills[phase_id])
            else:
                script[(phase_id, sid)] = _phase_default(phase_id)
    return script


def make_town_win_script(
    *,
    mafia_ids: Sequence[str],
    town_ids: Sequence[str],
    doctor_id: str,
    detective_id: str,
) -> dict[tuple[str, str], AgentResponse]:
    """Full mini7_v1 script that resolves to a TOWN win.

    Strategy: D1 vote eliminates ``mafia_ids[0]``; on N1 the doctor protects
    the surviving mafia's target so no kill lands; D2 vote eliminates
    ``mafia_ids[1]`` and the engine terminates with ``winner == 'TOWN'``.
    """
    if len(mafia_ids) < 2:
        raise ValueError("mini7_v1 has exactly 2 mafia seats")
    if doctor_id not in town_ids or detective_id not in town_ids:
        raise ValueError("doctor_id and detective_id must appear in town_ids")

    phase_ids = mini7_phase_ids()
    all_seats = list(mafia_ids) + list(town_ids)
    script: dict[tuple[str, str], AgentResponse] = {
        (p, s): _phase_default(p) for p in phase_ids for s in all_seats
    }

    for sid in town_ids:
        script[("DAY_1_VOTE", sid)] = _response(ActionType.VOTE, mafia_ids[0])

    protect_target = next(t for t in town_ids if t != doctor_id)
    for mid in mafia_ids:
        script[("NIGHT_1_ACTIONS", mid)] = _response(ActionType.MAFIA_KILL, protect_target)
    script[("NIGHT_1_ACTIONS", doctor_id)] = _response(ActionType.PROTECT, protect_target)
    script[("NIGHT_1_ACTIONS", detective_id)] = _response(ActionType.INVESTIGATE, mafia_ids[1])

    for sid in town_ids:
        script[("DAY_2_VOTE", sid)] = _response(ActionType.VOTE, mafia_ids[1])

    return script


def make_mafia_win_script(
    *,
    mafia_ids: Sequence[str],
    town_ids: Sequence[str],
) -> dict[tuple[str, str], AgentResponse]:
    """Full mini7_v1 script that resolves to a MAFIA win.

    Strategy: town abstains every day vote; mafia kills one town seat per
    night for three consecutive nights → parity at day 4 → ``winner == 'MAFIA'``.
    """
    if len(mafia_ids) < 2 or len(town_ids) < 3:
        raise ValueError("mini7_v1 expects 2 mafia and 5 town seats")

    phase_ids = mini7_phase_ids()
    all_seats = list(mafia_ids) + list(town_ids)
    script: dict[tuple[str, str], AgentResponse] = {
        (p, s): _phase_default(p) for p in phase_ids for s in all_seats
    }

    night_targets = {
        "NIGHT_1_ACTIONS": town_ids[0],
        "NIGHT_2_ACTIONS": town_ids[1],
        "NIGHT_3_ACTIONS": town_ids[2],
    }
    for phase_id, target in night_targets.items():
        for mid in mafia_ids:
            script[(phase_id, mid)] = _response(ActionType.MAFIA_KILL, target)

    return script


__all__ = [
    "make_mafia_script",
    "make_mafia_win_script",
    "make_town_win_script",
    "make_villager_script",
    "mini7_phase_ids",
]


def _use_postgres(config: pytest.Config) -> bool:
    import os

    if config.getoption("--postgres"):
        return True
    return bool(os.environ.get("PADRINO_TEST_DB_URL"))


def _postgres_url(config: pytest.Config) -> str:
    import os

    url = os.environ.get("PADRINO_TEST_DB_URL")
    if url:
        return url
    return "postgresql+asyncpg://padrino:padrino@localhost:5432/padrino_test"


@pytest.fixture(scope="session")
def use_postgres(pytestconfig: pytest.Config) -> bool:
    return _use_postgres(pytestconfig)


@pytest.fixture(scope="session")
async def db_engine(pytestconfig: pytest.Config) -> AsyncIterator[AsyncEngine]:
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config as AlembicConfig

    from padrino.db.base import Base, create_engine

    use_pg = _use_postgres(pytestconfig)
    if use_pg:
        pg_url = _postgres_url(pytestconfig)

        # Clear out any existing schema in public schema first to ensure clean state
        from sqlalchemy import create_engine as sync_create_engine

        sync_url = pg_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        sync_eng = sync_create_engine(sync_url)
        try:
            with sync_eng.connect() as conn:
                conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                conn.commit()
        except Exception:
            pass
        finally:
            sync_eng.dispose()

        # Run migrations once via Alembic. ``command.upgrade`` is synchronous
        # and Alembic's env.py drives the async engine with ``asyncio.run``,
        # which cannot be nested inside this already-running event loop (this
        # fixture is ``async def``). Run it on a worker thread so it gets its
        # own loop.
        import asyncio

        previous = os.environ.get("PADRINO_DB_URL")
        os.environ["PADRINO_DB_URL"] = pg_url
        try:
            migrations_pkg = (
                Path(__file__).resolve().parents[1] / "src" / "padrino" / "db" / "migrations"
            )
            cfg = AlembicConfig()
            cfg.set_main_option("script_location", str(migrations_pkg))
            cfg.set_main_option("sqlalchemy.url", pg_url)
            await asyncio.to_thread(command.upgrade, cfg, "head")
        finally:
            if previous is None:
                os.environ.pop("PADRINO_DB_URL", None)
            else:
                os.environ["PADRINO_DB_URL"] = previous

        # NullPool: this engine is session-scoped but pytest-asyncio runs each
        # test on a fresh function-scoped event loop. A pooled asyncpg
        # connection is bound to the loop that created it, so reusing it from a
        # later test's loop raises "Future attached to a different loop".
        # NullPool opens a fresh connection per checkout on the current loop.
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        eng = create_async_engine(pg_url, future=True, poolclass=NullPool)
        try:
            yield eng
        finally:
            await eng.dispose()
    else:
        eng = create_engine("sqlite+aiosqlite:///:memory:")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            yield eng
        finally:
            await eng.dispose()


@pytest.fixture
async def engine(db_engine: AsyncEngine, use_postgres: bool) -> AsyncIterator[AsyncEngine]:
    import sqlalchemy as sa

    from padrino.db.base import Base

    async with db_engine.connect() as conn:
        if not use_postgres:
            await conn.execute(sa.text("PRAGMA foreign_keys = OFF;"))

        # Tables are deleted children-first (reversed dependency order) so no
        # cascade is needed on either dialect.
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(sa.text(f'DELETE FROM "{table.name}";'))
        await conn.commit()

        if not use_postgres:
            await conn.execute(sa.text("PRAGMA foreign_keys = ON;"))
            await conn.commit()

    yield db_engine


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    from padrino.db.base import create_session_factory

    return create_session_factory(engine)
