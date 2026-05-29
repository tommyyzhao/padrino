"""Heterogeneous real-LLM gauntlet integration test (US-083).

Runs ONE mini7_v1 game in which every seat is a DISTINCT model identity —
the story that turns Padrino from a self-play stress test into a head-to-head
benchmark. The per-seat adapters are assembled by
:func:`padrino.gauntlets.heterogeneous.build_heterogeneous_adapter` and
multiplexed by :class:`padrino.llm.multiplex.SeatMultiplexAdapter`.

Roster (z.ai removed per the wave-4 operator decision 2026-05-28; Xiaomi
Mimo runs on free credits so the two surplus seats repeat it at ~no cost)::

    P01 Cerebras  GLM-4.7              (paid)
    P02 DeepInfra DeepSeek-V4-Flash    (paid)
    P03 DeepInfra Gemma-4-26B-A4B-it   (paid)
    P04 Xiaomi    Mimo-v2.5            (free)
    P05 Xiaomi    Mimo-v2.5-pro        (free)
    P06 Xiaomi    Mimo-v2.5    (repeat, free)
    P07 Xiaomi    Mimo-v2.5-pro (repeat, free)

Marked ``@pytest.mark.integration`` and skipped unless the three providers
actually seated — CEREBRAS / DEEPINFRA / XIAOMI — all have keys. (The AC text
listed ZAI_API_KEY too, but z.ai is no longer in the roster, so requiring it
would be dead weight.)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Gauntlet
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.evaluation import evaluate_gauntlet
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.gauntlets.scheduler import create_gauntlet, derive_game_seed
from padrino.llm.adapter import AdapterResult, AdapterStatus, AgentBuild
from padrino.llm.prompts import (
    CANONICAL_RESPONSE_SCHEMA,
    CANONICAL_VERSION,
    iter_canonical_prompts,
)
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from padrino.settings import Settings

# One game, mostly free seats: ~25-70 calls, only ~3/7 of them paid
# (Cerebras + two DeepInfra seats). $4 leaves generous headroom.
_COST_CAP_USD = 4.00
_PARSE_RATE_GATE = 0.70
_TERMINAL_RESULTS = frozenset({"TOWN", "MAFIA", "DRAW"})
_PARSED_OK_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"ok", "fallback_ok", "same_model_fallback_ok"}
)
_FAILURE_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"provider_error", "primary_failed", "both_failed", "fallback_ok"}
)
_REQUIRED_KEYS = ("CEREBRAS_API_KEY", "DEEPINFRA_API_KEY", "XIAOMI_API_KEY")

# (provider, litellm_model_id, display_name). The provider string must match
# a key in ``padrino.gauntlets.heterogeneous.provider_endpoints``.
_GLM = ("cerebras", "cerebras/zai-glm-4.7", "Cerebras GLM-4.7")
_DEEPSEEK = ("deepinfra", "deepinfra/deepseek-ai/DeepSeek-V4-Flash", "DeepInfra DeepSeek-V4-Flash")
_GEMMA = ("deepinfra", "deepinfra/google/gemma-4-26B-A4B-it", "DeepInfra Gemma-4-26B-A4B-it")
_MIMO = ("xiaomi", "openai/mimo-v2.5", "Xiaomi Mimo-v2.5")
_MIMO_PRO = ("xiaomi", "openai/mimo-v2.5-pro", "Xiaomi Mimo-v2.5-pro")

# Seat -> distinct model. P06/P07 repeat the free Xiaomi seats.
_ROSTER: dict[str, tuple[str, str, str]] = {
    "P01": _GLM,
    "P02": _DEEPSEEK,
    "P03": _GEMMA,
    "P04": _MIMO,
    "P05": _MIMO_PRO,
    "P06": _MIMO,
    "P07": _MIMO_PRO,
}
_HET_ADAPTER_VERSION = "heterogeneous-v1"


async def _seed_heterogeneous_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed providers / model_configs / agent_builds for every distinct model.

    Returns ``(league_id, prompt_version_id, agent_build_id_by_model_id)`` where
    the last maps each distinct ``litellm_model_id`` to its DB agent_build id.
    """
    async with session_factory() as session, session.begin():
        # Canonical prompt rows (one per role family); the gauntlet FK points at
        # VANILLA_TOWN as in the demo seeder.
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
            session,
            name="Heterogeneous League",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )

        # One ModelProvider row per provider name (shared across its models).
        provider_ids: dict[str, uuid.UUID] = {}
        provider_bases = {
            "cerebras": (None, "env:CEREBRAS_API_KEY"),
            "deepinfra": ("https://api.deepinfra.com/v1/openai", "env:DEEPINFRA_API_KEY"),
            "xiaomi": (Settings().xiaomi_base_url, "env:XIAOMI_API_KEY"),
        }
        for name, (base_url, auth_ref) in provider_bases.items():
            provider = await providers_repo.create(
                session, name=name, auth_secret_ref=auth_ref, base_url=base_url
            )
            provider_ids[name] = provider.id

        agent_build_by_model: dict[str, uuid.UUID] = {}
        for provider_name, litellm_model_id, display_name in set(_ROSTER.values()):
            mc = await model_configs_repo.create(
                session,
                provider_id=provider_ids[provider_name],
                model_name=litellm_model_id,
                litellm_model_id=litellm_model_id,
                default_temperature=mini7_v1.TEMPERATURE,
                default_top_p=mini7_v1.TOP_P,
                default_max_output_tokens=4096,
                supports_structured_outputs=True,
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=display_name,
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version=_HET_ADAPTER_VERSION,
                inference_params={},
                active=True,
            )
            agent_build_by_model[litellm_model_id] = ab.id
        return league.id, pv.id, agent_build_by_model


@pytest.mark.integration
async def test_heterogeneous_gauntlet_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One mini7_v1 game with a distinct model in every seat, real providers."""
    load_dotenv(override=False)
    missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing provider keys {missing}; skipping heterogeneous gauntlet")

    settings = Settings()
    db_path = tmp_path / "heterogeneous.sqlite"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)

        league_id, prompt_version_id, ab_by_model = await _seed_heterogeneous_admin(session_factory)

        # seat -> DB agent_build id (for ranking attribution) and seat -> llm
        # AgentBuild value object (for adapter construction).
        agent_builds_by_seat: dict[str, uuid.UUID] = {}
        agent_build_assignments: dict[str, AgentBuild] = {}
        for seat, (provider_name, litellm_model_id, _display) in _ROSTER.items():
            agent_builds_by_seat[seat] = ab_by_model[litellm_model_id]
            agent_build_assignments[seat] = AgentBuild(
                provider=provider_name,
                model_id=litellm_model_id,
                prompt_version=CANONICAL_VERSION,
                inference_params={
                    "temperature": settings.padrino_temperature,
                    "top_p": settings.padrino_top_p,
                },
                adapter_version=_HET_ADAPTER_VERSION,
            )

        roster = [agent_builds_by_seat[f"P{i + 1:02d}"] for i in range(mini7_v1.PLAYER_COUNT)]
        gauntlet_seed = "integration-heterogeneous-001"
        async with session_factory() as session:
            created = await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=prompt_version_id,
                clone_count=1,
                gauntlet_seed=gauntlet_seed,
                roster=roster,
            )
        assert len(created.game_ids) == 1

        mux = build_heterogeneous_adapter(agent_build_assignments, settings=settings)
        game_id = created.game_ids[0]
        config = GameConfig(
            game_id=str(game_id),
            game_seed=derive_game_seed(gauntlet_seed, 0),
            ruleset_id=mini7_v1.RULESET_ID,
            timeout_s=float(settings.padrino_llm_timeout_seconds),
        )
        persistence = GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=agent_builds_by_seat,
            league_id=league_id,
        )
        outcome = await run_game(config, mux, ranked=True, persistence=persistence)

        # (a) terminal state — heterogeneous play means any of TOWN/MAFIA/DRAW.
        winner = outcome.final_state.terminal_result
        assert winner in _TERMINAL_RESULTS, (
            f"game did not reach a terminal state; terminal_result={winner!r} "
            f"reason={outcome.final_state.terminal_reason!r}"
        )

        # (b) parse rate over every adapter call.
        all_llm_calls: list[AdapterResult] = list(outcome.llm_calls)
        assert all_llm_calls, "expected at least one adapter call"
        for index, call in enumerate(all_llm_calls):
            assert call.raw_response or call.status in _FAILURE_STATUSES, (
                f"call #{index}: empty raw_response without a failure status; "
                f"status={call.status!r}"
            )
        parsed_ok = sum(1 for c in all_llm_calls if c.status in _PARSED_OK_STATUSES)
        parse_rate = parsed_ok / len(all_llm_calls)
        assert parse_rate >= _PARSE_RATE_GATE, (
            f"parse rate {parsed_ok}/{len(all_llm_calls)} ({parse_rate:.0%}) below gate; "
            f"status histogram={sorted({c.status for c in all_llm_calls})}"
        )

        # (c) per-model coverage: every distinct model produced >=1 parsed-OK
        # call. Attribution comes from the multiplex's per-seat accumulator
        # (AdapterResult carries no seat id).
        ok_model_ids: set[str] = set()
        for seat, results in mux.calls_by_seat.items():
            model_id = agent_build_assignments[seat].model_id
            if any(r.status in _PARSED_OK_STATUSES for r in results):
                ok_model_ids.add(model_id)
        distinct_model_ids = {b.model_id for b in agent_build_assignments.values()}
        missing_coverage = distinct_model_ids - ok_model_ids
        assert not missing_coverage, (
            f"these models produced no parsed-OK call: {sorted(missing_coverage)}"
        )

        # Finalize + evaluate.
        async with session_factory() as session:
            finalized = await finalize_gauntlet_if_done(session, created.gauntlet_id)
        assert finalized is not None and finalized.status == "COMPLETED"

        async with session_factory() as session:
            gauntlet_row = await session.get(Gauntlet, created.gauntlet_id)
            assert gauntlet_row is not None and gauntlet_row.status == "COMPLETED"
            report = await evaluate_gauntlet(created.gauntlet_id, session)
        assert report is not None, "evaluate_gauntlet returned None for a completed gauntlet"

        # Every distinct model must appear in rating_deltas with non-default
        # mu/sigma (a single game still moves sigma off its prior for everyone).
        distinct_build_ids = set(agent_builds_by_seat.values())
        deltas_by_build: dict[uuid.UUID, list[Any]] = {}
        for delta in report.rating_deltas:
            deltas_by_build.setdefault(delta.agent_build_id, []).append(delta)
        for build_id in distinct_build_ids:
            assert build_id in deltas_by_build, f"agent_build {build_id} absent from rating_deltas"
            moved = any(
                (d.post_mu != INITIAL_MU) or (d.post_sigma != INITIAL_SIGMA)
                for d in deltas_by_build[build_id]
            )
            assert moved, f"agent_build {build_id} rating did not move off OpenSkill defaults"

        total_cost = sum((c.cost_usd or 0.0) for c in all_llm_calls)
        with capsys.disabled():
            print(
                f"\n[US-083] heterogeneous gauntlet: winner={winner} "
                f"calls={len(all_llm_calls)} parse_rate={parse_rate:.0%} "
                f"models_with_ok={len(ok_model_ids)}/{len(distinct_model_ids)} "
                f"cost=${total_cost:.4f} (cap=${_COST_CAP_USD:.2f})"
            )
        assert total_cost <= _COST_CAP_USD, (
            f"total cost ${total_cost:.4f} exceeded ${_COST_CAP_USD:.2f} cap"
        )
    finally:
        await engine.dispose()
