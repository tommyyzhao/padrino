"""Tests for the scheduled-gauntlet job (US-085) using mock adapters.

No provider keys / network: the tournament runs through NoopMockAdapter (happy
path) or a cost-stamping wrapper (cost-cap-abort path).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.models import AgentBuild as AgentBuildRow
from padrino.db.models import Gauntlet, League, ScheduledGauntlet
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories import scheduled_gauntlets as scheduled_gauntlets_repo
from padrino.gauntlets.tournament import AdapterFactory, TournamentResult
from padrino.llm.adapter import AdapterResult
from padrino.llm.mock import NoopMockAdapter
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts
from padrino.scheduler import gauntlet_job
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


@dataclass(frozen=True, slots=True)
class _RecordedTournamentCall:
    league_id: uuid.UUID
    gauntlet_seed: str
    roster_by_seat: dict[str, uuid.UUID]
    n_games: int
    cost_cap_usd: float | None


class _TournamentRecorder:
    def __init__(self) -> None:
        self.calls: list[_RecordedTournamentCall] = []

    async def __call__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        league_id: uuid.UUID,
        gauntlet_seed: str,
        roster_by_seat: Mapping[str, uuid.UUID],
        n_games: int,
        settings: Settings,
        cost_cap_usd: float | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> tuple[uuid.UUID, TournamentResult]:
        del settings, adapter_factory
        first_build_id = next(iter(roster_by_seat.values()))
        async with session_factory() as session, session.begin():
            league = await session.get(League, league_id)
            first_build = await session.get(AgentBuildRow, first_build_id)
            assert league is not None
            assert first_build is not None

            gauntlet = await gauntlets_repo.create(
                session,
                league_id=league_id,
                ruleset_id=league.ruleset_id,
                prompt_version_id=first_build.prompt_version_id,
                clone_count=n_games,
                gauntlet_seed=gauntlet_seed,
                ranked=True,
            )
            gauntlet_id = gauntlet.id

        self.calls.append(
            _RecordedTournamentCall(
                league_id=league_id,
                gauntlet_seed=gauntlet_seed,
                roster_by_seat=dict(roster_by_seat),
                n_games=n_games,
                cost_cap_usd=cost_cap_usd,
            )
        )
        return (
            gauntlet_id,
            TournamentResult(
                outcomes=(),
                games_run=n_games,
                total_cost_usd=0.0,
                cost_capped=False,
            ),
        )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _seed_schedule(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cost_cap_usd: float,
    enabled: bool = True,
    n_games: int = 1,
    next_run_at: datetime | None = None,
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
            n_games=n_games,
            cost_cap_usd=cost_cap_usd,
            enabled=enabled,
            next_run_at=next_run_at,
        )
        return sched.id


async def test_scheduled_seed_uses_fire_instant_not_injected_now(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _TournamentRecorder()
    monkeypatch.setattr(gauntlet_job, "run_tournament_from_roster", recorder)
    scheduled_fire_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    sched_id = await _seed_schedule(
        session_factory,
        cost_cap_usd=10.0,
        next_run_at=scheduled_fire_at,
    )

    first_now = scheduled_fire_at + timedelta(minutes=1)
    await run_due_scheduled_gauntlets(
        session_factory,
        now=first_now,
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    async with session_factory() as session, session.begin():
        sched = await session.get(ScheduledGauntlet, sched_id)
        assert sched is not None
        sched.next_run_at = scheduled_fire_at
        sched.last_run_at = None
        sched.last_run_gauntlet_id = None

    second_now = scheduled_fire_at + timedelta(hours=6)
    await run_due_scheduled_gauntlets(
        session_factory,
        now=second_now,
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert len(recorder.calls) == 2
    assert recorder.calls[0].gauntlet_seed == recorder.calls[1].gauntlet_seed
    assert recorder.calls[0].gauntlet_seed == gauntlet_job.derive_scheduled_gauntlet_seed(
        sched_id,
        scheduled_fire_at=scheduled_fire_at,
    )


async def test_scheduled_seed_changes_between_distinct_occurrences(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _TournamentRecorder()
    monkeypatch.setattr(gauntlet_job, "run_tournament_from_roster", recorder)
    scheduled_fire_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    sched_id = await _seed_schedule(
        session_factory,
        cost_cap_usd=10.0,
        next_run_at=scheduled_fire_at,
    )

    await run_due_scheduled_gauntlets(
        session_factory,
        now=scheduled_fire_at + timedelta(minutes=1),
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )
    async with session_factory() as session:
        sched = await session.get(ScheduledGauntlet, sched_id)
        assert sched is not None
        assert sched.next_run_at is not None
        next_occurrence = _aware(sched.next_run_at)

    await run_due_scheduled_gauntlets(
        session_factory,
        now=next_occurrence + timedelta(seconds=1),
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert len(recorder.calls) == 2
    assert recorder.calls[0].gauntlet_seed == gauntlet_job.derive_scheduled_gauntlet_seed(
        sched_id,
        scheduled_fire_at=scheduled_fire_at,
    )
    assert recorder.calls[1].gauntlet_seed == gauntlet_job.derive_scheduled_gauntlet_seed(
        sched_id,
        scheduled_fire_at=next_occurrence,
    )
    assert recorder.calls[0].gauntlet_seed != recorder.calls[1].gauntlet_seed


async def test_scheduled_gauntlet_shape_is_unchanged_by_seed_source(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _TournamentRecorder()
    monkeypatch.setattr(gauntlet_job, "run_tournament_from_roster", recorder)
    scheduled_fire_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    sched_id = await _seed_schedule(
        session_factory,
        cost_cap_usd=12.5,
        n_games=3,
        next_run_at=scheduled_fire_at,
    )

    runs = await run_due_scheduled_gauntlets(
        session_factory,
        now=scheduled_fire_at + timedelta(minutes=1),
        settings=Settings(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert len(runs) == 1
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.n_games == 3
    assert call.cost_cap_usd == 12.5
    assert set(call.roster_by_seat) == set(_SEATS)
    async with session_factory() as session:
        sched = await session.get(ScheduledGauntlet, sched_id)
        gauntlet = await session.get(Gauntlet, runs[0].gauntlet_id)
        assert sched is not None
        assert gauntlet is not None
        assert sched.last_run_gauntlet_id == gauntlet.id
        assert gauntlet.league_id == call.league_id
        assert gauntlet.ruleset_id == mini7_v1.RULESET_ID
        assert gauntlet.clone_count == 3
        assert gauntlet.gauntlet_seed == call.gauntlet_seed


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
