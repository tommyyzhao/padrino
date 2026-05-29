"""Tests for the scheduled-gauntlet job (US-085) using mock adapters.

No provider keys / network: the tournament runs through NoopMockAdapter (happy
path) or a cost-stamping wrapper (cost-cap-abort path).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Gauntlet, ScheduledGauntlet
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories import scheduled_gauntlets as scheduled_gauntlets_repo
from padrino.llm.adapter import AdapterResult
from padrino.llm.mock import NoopMockAdapter
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts
from padrino.scheduler.gauntlet_job import STATUS_COST_CAPPED, run_due_scheduled_gauntlets
from padrino.settings import Settings

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]
_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


class _CostAdapter:
    def __init__(self, cost: float) -> None:
        self._inner = NoopMockAdapter()
        self._cost = cost

    async def complete(self, observation: Observation) -> AdapterResult:
        result = await self._inner.complete(observation)
        return result.model_copy(update={"cost_usd": self._cost})


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_schedule(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cost_cap_usd: float,
    enabled: bool = True,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        canonical: dict[str, Any] = {}
        for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
            canonical[template.role_family.value] = await prompt_versions_repo.create(
                session,
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
        pv = canonical["VANILLA_TOWN"]
        league = await leagues_repo.create(
            session, name="Job League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        provider = await providers_repo.create(
            session, name="mockprov", auth_secret_ref="env:MOCK_KEY"
        )
        roster: dict[str, str] = {}
        for seat in _SEATS:
            mc = await model_configs_repo.create(
                session,
                provider_id=provider.id,
                model_name=f"mock/{seat}",
                litellm_model_id=f"mock/{seat}",
                default_temperature=0.7,
                default_top_p=1.0,
                default_max_output_tokens=512,
                supports_structured_outputs=True,
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"Mock {seat}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="mock-v1",
                inference_params={},
                active=True,
            )
            roster[seat] = str(ab.id)
        sched = await scheduled_gauntlets_repo.create(
            session,
            name="job-sched",
            schedule_cron="* * * * *",
            roster_spec_json={"league_id": str(league.id), "roster": roster},
            n_games=1,
            cost_cap_usd=cost_cap_usd,
            enabled=enabled,
            next_run_at=None,
        )
        return sched.id


async def test_happy_path_runs_finalizes_and_reschedules(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sched_id = await _seed_schedule(session_factory, cost_cap_usd=10.0)
    runs = await run_due_scheduled_gauntlets(
        session_factory,
        now=_NOW,
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )
    assert len(runs) == 1
    run = runs[0]
    assert not run.cost_capped

    async with session_factory() as session:
        gauntlet = await session.get(Gauntlet, run.gauntlet_id)
        assert gauntlet is not None and gauntlet.status == "COMPLETED"
        sched = await session.get(ScheduledGauntlet, sched_id)
        assert sched is not None
        assert sched.last_run_at is not None
        assert sched.last_run_gauntlet_id == run.gauntlet_id
        # next_run_at recomputed strictly after now (cron "* * * * *" -> +1 min).
        # SQLite returns naive datetimes, so coerce to UTC before comparing.
        assert sched.next_run_at is not None
        nxt = sched.next_run_at
        nxt = nxt if nxt.tzinfo is not None else nxt.replace(tzinfo=UTC)
        assert nxt > _NOW


async def test_cost_cap_abort_marks_gauntlet_cost_capped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sched_id = await _seed_schedule(session_factory, cost_cap_usd=0.005)
    runs = await run_due_scheduled_gauntlets(
        session_factory,
        now=_NOW,
        settings=Settings(),
        adapter_factory=lambda _a: _CostAdapter(0.01),
    )
    assert len(runs) == 1
    assert runs[0].cost_capped

    async with session_factory() as session:
        gauntlet = await session.get(Gauntlet, runs[0].gauntlet_id)
        assert gauntlet is not None and gauntlet.status == STATUS_COST_CAPPED
        sched = await session.get(ScheduledGauntlet, sched_id)
        assert sched is not None and sched.last_run_gauntlet_id == runs[0].gauntlet_id


async def test_disabled_schedule_is_not_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_schedule(session_factory, cost_cap_usd=10.0, enabled=False)
    runs = await run_due_scheduled_gauntlets(
        session_factory,
        now=_NOW,
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )
    assert runs == []
