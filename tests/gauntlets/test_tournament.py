"""Non-integration tests for the multi-game tournament runner (US-084).

Uses mock adapters (no provider keys, no network) to exercise the
permutation threading, multi-game completion, the cost-cap early stop, and the
new per-model evaluation fields end-to-end through real DB persistence.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import GameSeat
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.evaluation import evaluate_gauntlet
from padrino.gauntlets.scheduler import create_gauntlet
from padrino.gauntlets.tournament import (
    project_agent_build,
    run_heterogeneous_tournament,
    run_tournament_from_roster,
)
from padrino.llm.adapter import AdapterResult, AgentBuild
from padrino.llm.mock import NoopMockAdapter
from padrino.llm.prompts import (
    CANONICAL_RESPONSE_SCHEMA,
    CANONICAL_VERSION,
    iter_canonical_prompts,
)
from padrino.settings import Settings

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]


class _CostAdapter:
    """NoopMockAdapter wrapper that stamps a fixed per-call cost."""

    def __init__(self, cost: float) -> None:
        self._inner = NoopMockAdapter()
        self._cost = cost

    async def complete(self, observation: Observation) -> AdapterResult:
        result = await self._inner.complete(observation)
        return result.model_copy(update={"cost_usd": self._cost})


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID], dict[str, AgentBuild]]:
    """Seed 7 distinct agent_builds (one per seat) under one mock provider."""
    async with session_factory() as session, session.begin():
        canonical_rows: dict[str, Any] = {}
        for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
            row = await prompt_versions_repo.create(
                session,
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
            canonical_rows[template.role_family.value] = row
        pv = canonical_rows["VANILLA_TOWN"]
        league = await leagues_repo.create(
            session, name="Tournament League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        provider = await providers_repo.create(
            session, name="mockprov", auth_secret_ref="env:MOCK_KEY"
        )
        builds_by_seat: dict[str, uuid.UUID] = {}
        assignments: dict[str, AgentBuild] = {}
        for seat in _SEATS:
            model_id = f"mock/model-{seat}"
            mc = await model_configs_repo.create(
                session,
                provider_id=provider.id,
                model_name=model_id,
                litellm_model_id=model_id,
                default_temperature=mini7_v1.TEMPERATURE,
                default_top_p=mini7_v1.TOP_P,
                default_max_output_tokens=512,
                supports_structured_outputs=True,
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"Mock {seat}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="mock-tournament-v1",
                inference_params={},
                active=True,
            )
            builds_by_seat[seat] = ab.id
            assignments[seat] = AgentBuild(
                provider="mockprov",
                model_id=model_id,
                prompt_version=CANONICAL_VERSION,
                inference_params={},
                adapter_version="mock-tournament-v1",
            )
        return league.id, pv.id, builds_by_seat, assignments


async def test_tournament_runs_all_games_and_aggregates_per_model(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'tournament.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        league_id, pv_id, builds_by_seat, assignments = await _seed(session_factory)

        n_games = 3
        roster = [builds_by_seat[s] for s in _SEATS]
        seed = "tournament-mock-001"
        async with session_factory() as session:
            created = await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=n_games,
                gauntlet_seed=seed,
                roster=roster,
            )

        result = await run_heterogeneous_tournament(
            session_factory=session_factory,
            league_id=league_id,
            gauntlet_seed=seed,
            game_ids=created.game_ids,
            base_agent_builds_by_seat=builds_by_seat,
            base_agent_build_assignments=assignments,
            settings=Settings(),
            adapter_factory=lambda _assign: NoopMockAdapter(),
        )

        assert result.games_run == n_games
        assert not result.cost_capped
        for outcome in result.outcomes:
            assert outcome.final_state.terminal_result in {"TOWN", "MAFIA", "DRAW"}

        # Permutation actually rotated seats: across the 3 games at least one
        # build occupied more than one distinct seat index.
        async with session_factory() as session:
            seat_rows = list(
                (
                    await session.execute(
                        select(GameSeat).where(GameSeat.game_id.in_(list(created.game_ids)))
                    )
                )
                .scalars()
                .all()
            )
        positions: dict[uuid.UUID, set[int]] = {}
        for row in seat_rows:
            positions.setdefault(row.agent_build_id, set()).add(row.seat_index)
        assert any(len(idxs) > 1 for idxs in positions.values()), (
            "no build changed seats across games — permutation not applied"
        )
        assert len(seat_rows) == n_games * mini7_v1.PLAYER_COUNT

        async with session_factory() as session:
            await finalize_gauntlet_if_done(session, created.gauntlet_id)
            report = await evaluate_gauntlet(created.gauntlet_id, session)
        assert report is not None

        # New US-084 fields: one seat-count row per distinct build, totals add up.
        assert len(report.faction_seat_counts) == mini7_v1.PLAYER_COUNT
        total_seats = sum(m.total_seats for m in report.faction_seat_counts)
        assert total_seats == n_games * mini7_v1.PLAYER_COUNT
        for m in report.faction_seat_counts:
            assert m.total_seats == m.town_seats + m.mafia_seats
        # model_faction_breakdown carries a Wilson band per (model, faction) seen.
        assert report.model_faction_breakdown
        for entry in report.model_faction_breakdown:
            assert entry.faction in {"TOWN", "MAFIA"}
            assert 0.0 <= entry.win_rate.lower <= entry.win_rate.upper <= 1.0
    finally:
        await engine.dispose()


async def test_tournament_cost_cap_stops_early(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cap.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        league_id, pv_id, builds_by_seat, assignments = await _seed(session_factory)

        roster = [builds_by_seat[s] for s in _SEATS]
        seed = "tournament-cap-001"
        async with session_factory() as session:
            created = await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=3,
                gauntlet_seed=seed,
                roster=roster,
            )

        # Each call costs $0.01; one game has many calls, so a $0.05 cap trips
        # after the first game completes.
        result = await run_heterogeneous_tournament(
            session_factory=session_factory,
            league_id=league_id,
            gauntlet_seed=seed,
            game_ids=created.game_ids,
            base_agent_builds_by_seat=builds_by_seat,
            base_agent_build_assignments=assignments,
            settings=Settings(),
            cost_cap_usd=0.05,
            adapter_factory=lambda _assign: _CostAdapter(0.01),
        )
        assert result.cost_capped
        assert result.games_run < 3
        assert result.total_cost_usd > 0.05
    finally:
        await engine.dispose()


async def test_project_agent_build_flattens_chain(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'project.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        _league_id, _pv_id, builds_by_seat, _assignments = await _seed(session_factory)

        async with session_factory() as session:
            build = await project_agent_build(session, builds_by_seat["P01"])
        assert build.provider == "mockprov"
        assert build.model_id == "mock/model-P01"
        assert build.prompt_version == CANONICAL_VERSION
        # model_config defaults flow into inference_params.
        assert build.inference_params["temperature"] == pytest.approx(mini7_v1.TEMPERATURE)
        assert build.inference_params["top_p"] == pytest.approx(mini7_v1.TOP_P)
    finally:
        await engine.dispose()


async def test_run_tournament_from_roster_projects_and_runs(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'roster.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        league_id, _pv_id, builds_by_seat, _assignments = await _seed(session_factory)

        gauntlet_id, result = await run_tournament_from_roster(
            session_factory=session_factory,
            league_id=league_id,
            gauntlet_seed="from-roster-001",
            roster_by_seat=builds_by_seat,
            n_games=2,
            settings=Settings(),
            adapter_factory=lambda _assign: NoopMockAdapter(),
        )
        assert result.games_run == 2
        async with session_factory() as session:
            await finalize_gauntlet_if_done(session, gauntlet_id)
            report = await evaluate_gauntlet(gauntlet_id, session)
        assert report is not None
        assert report.faction_seat_counts
    finally:
        await engine.dispose()


async def test_run_tournament_from_roster_rejects_wrong_seats(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'wrong.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        league_id, _pv_id, builds_by_seat, _assignments = await _seed(session_factory)
        partial = {seat: builds_by_seat[seat] for seat in _SEATS[:6]}
        with pytest.raises(ValueError, match="roster must cover"):
            await run_tournament_from_roster(
                session_factory=session_factory,
                league_id=league_id,
                gauntlet_seed="bad",
                roster_by_seat=partial,
                n_games=1,
                settings=Settings(),
                adapter_factory=lambda _assign: NoopMockAdapter(),
            )
    finally:
        await engine.dispose()
