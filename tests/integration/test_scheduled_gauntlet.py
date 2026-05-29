"""Scheduled-gauntlet integration test (US-085).

Seeds the real heterogeneous roster + a one-shot scheduled gauntlet (cron
``* * * * *``, n_games=1, $1 cap), advances the injected scheduler clock by
60 s, and asserts the job fired a real gauntlet with at least one completed
game and updated the schedule's run metadata.

Marked ``@pytest.mark.integration`` and skipped unless CEREBRAS / DEEPINFRA /
XIAOMI keys are all present (z.ai removed from the roster per the 2026-05-28
operator decision; the AC's ZAI_API_KEY gate would be dead weight).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Game, Gauntlet, ScheduledGauntlet
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories import scheduled_gauntlets as scheduled_gauntlets_repo
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts
from padrino.scheduler.gauntlet_job import run_due_scheduled_gauntlets
from padrino.settings import Settings

_REQUIRED_KEYS = ("CEREBRAS_API_KEY", "DEEPINFRA_API_KEY", "XIAOMI_API_KEY")

# Same z.ai-free roster as US-083: 5 distinct models, 2 free Xiaomi repeats.
_GLM = ("cerebras", "cerebras/zai-glm-4.7")
_DEEPSEEK = ("deepinfra", "deepinfra/deepseek-ai/DeepSeek-V4-Flash")
_GEMMA = ("deepinfra", "deepinfra/google/gemma-4-26B-A4B-it")
_MIMO = ("xiaomi", "openai/mimo-v2.5")
_MIMO_PRO = ("xiaomi", "openai/mimo-v2.5-pro")
_ROSTER = {
    "P01": _GLM,
    "P02": _DEEPSEEK,
    "P03": _GEMMA,
    "P04": _MIMO,
    "P05": _MIMO_PRO,
    "P06": _MIMO,
    "P07": _MIMO_PRO,
}


async def _seed(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
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
            session, name="Scheduled League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        provider_bases = {
            "cerebras": (None, "env:CEREBRAS_API_KEY"),
            "deepinfra": ("https://api.deepinfra.com/v1/openai", "env:DEEPINFRA_API_KEY"),
            "xiaomi": (Settings().xiaomi_base_url, "env:XIAOMI_API_KEY"),
        }
        provider_ids: dict[str, uuid.UUID] = {}
        for name, (base_url, auth_ref) in provider_bases.items():
            p = await providers_repo.create(
                session, name=name, auth_secret_ref=auth_ref, base_url=base_url
            )
            provider_ids[name] = p.id

        build_by_model: dict[str, uuid.UUID] = {}
        for provider_name, litellm_model_id in set(_ROSTER.values()):
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
                display_name=litellm_model_id,
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="scheduled-v1",
                inference_params={},
                active=True,
            )
            build_by_model[litellm_model_id] = ab.id

        roster = {seat: str(build_by_model[model]) for seat, (_p, model) in _ROSTER.items()}
        sched = await scheduled_gauntlets_repo.create(
            session,
            name="one-shot",
            schedule_cron="* * * * *",
            roster_spec_json={"league_id": str(league.id), "roster": roster},
            n_games=1,
            cost_cap_usd=1.0,
            enabled=True,
            next_run_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
        return sched.id


@pytest.mark.integration
async def test_one_shot_scheduled_gauntlet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load_dotenv(override=False)
    missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing provider keys {missing}; skipping scheduled gauntlet")

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduled.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        sched_id = await _seed(session_factory)

        # Advance the injected scheduler clock by 60 s past the scheduled time.
        base = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        now = base + timedelta(seconds=60)
        runs = await run_due_scheduled_gauntlets(session_factory, now=now, settings=Settings())

        assert len(runs) == 1, f"expected exactly one scheduled run, got {runs}"
        run = runs[0]

        async with session_factory() as session:
            sched = await session.get(ScheduledGauntlet, sched_id)
            assert sched is not None
            assert sched.last_run_at is not None
            assert sched.last_run_gauntlet_id == run.gauntlet_id

            gauntlet = await session.get(Gauntlet, run.gauntlet_id)
            assert gauntlet is not None
            completed_games = list(
                (
                    await session.execute(
                        select(Game).where(
                            Game.gauntlet_id == run.gauntlet_id, Game.status == "COMPLETED"
                        )
                    )
                )
                .scalars()
                .all()
            )

        with capsys.disabled():
            print(
                f"\n[US-085] scheduled gauntlet: gauntlet={run.gauntlet_id} "
                f"status={gauntlet.status} completed_games={len(completed_games)} "
                f"cost=${run.total_cost_usd:.4f} cost_capped={run.cost_capped}"
            )
        assert len(completed_games) >= 1, "scheduled gauntlet produced no completed game"
    finally:
        await engine.dispose()
