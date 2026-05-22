"""Real-LLM mini-gauntlet integration test (US-073).

Drives a three-clone gauntlet through ``create_gauntlet`` + ``run_game`` +
``finalize_gauntlet_if_done`` against the real Cerebras (primary) + DeepInfra
(fallback) providers, then asserts every game reached a terminal state, the
gauntlet flipped to ``COMPLETED``, OpenSkill rating rows moved off their
defaults across both GLOBAL and FACTION scopes, and the leaderboard ships a
non-empty entry with the expected provisional flag.

Like :mod:`tests.integration.test_real_providers_full_game`, this test is
marked ``@pytest.mark.integration`` and is skipped when
``CEREBRAS_API_KEY`` is not present, so default CI (``-m "not integration"``)
never hits a real provider.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import select

from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Gauntlet, Rating
from padrino.demo_gauntlet import (
    DEMO_ADAPTER_VERSION,
    DEMO_PROMPT_VERSION,
    _seed_minimal_admin,
)
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.scheduler import create_gauntlet, derive_game_seed
from padrino.leaderboards.service import compute_leaderboard
from padrino.llm.adapter import AdapterResult, AdapterStatus, AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.prompts import canonical_prompts_by_role
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_FACTION,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from padrino.settings import Settings

# Sized for 3 games x ~25-200 adapter calls per game x $0.005 per call with
# realistic completion lengths. The per-game cap in US-071 was $2.00 (full
# 5-day game); three of those is $6.00. Test prints the actual spend.
_COST_CAP_USD = 6.00
_CLONE_COUNT = 3
_PARSE_RATE_GATE = 0.70
_TERMINAL_RESULTS = frozenset({"TOWN", "MAFIA", "DRAW"})
# A "parsed-OK" call is one where the runner received a valid AgentResponse
# from ANY host: primary directly (`ok`), or the configured fallback host
# after the primary errored (`fallback_ok`). Both are real gameplay; only
# the failure / coercion paths are excluded. Counting `ok` alone would
# treat a healthy fallback path as a parse failure.
_PARSED_OK_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"ok", "fallback_ok", "same_model_fallback_ok"}
)
_FAILURE_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"provider_error", "primary_failed", "both_failed", "fallback_ok"}
)


def _build_adapter(settings: Settings) -> LiteLlmAdapter:
    routing = RoutingPolicy(
        primary_model=settings.padrino_primary_model,
        fallback_model=settings.padrino_fallback_model,
    )
    build = AgentBuild(
        provider="cerebras",
        model_id=settings.padrino_primary_model,
        prompt_version=DEMO_PROMPT_VERSION,
        inference_params={
            "temperature": settings.padrino_temperature,
            "top_p": settings.padrino_top_p,
        },
        adapter_version=DEMO_ADAPTER_VERSION,
    )
    return LiteLlmAdapter(
        routing_policy=routing,
        agent_build=build,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
        auth_secret_ref="env:CEREBRAS_API_KEY",
        system_prompts_by_role=canonical_prompts_by_role(),
    )


@pytest.mark.integration
async def test_real_providers_minigauntlet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run a 3-clone mini7_v1 gauntlet against real providers end-to-end."""
    load_dotenv(override=False)
    if not os.environ.get("CEREBRAS_API_KEY"):
        pytest.skip("CEREBRAS_API_KEY not set; skipping real-provider integration test")

    settings = Settings()
    db_path = tmp_path / "minigauntlet.sqlite"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)

        league_id, agent_build_id, prompt_version_id = await _seed_minimal_admin(
            session_factory, display_name="minigauntlet-build"
        )
        roster = [agent_build_id] * mini7_v1.PLAYER_COUNT
        gauntlet_seed = "integration-real-minigauntlet-001"
        async with session_factory() as session:
            created = await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=prompt_version_id,
                clone_count=_CLONE_COUNT,
                gauntlet_seed=gauntlet_seed,
                roster=roster,
            )
        assert len(created.game_ids) == _CLONE_COUNT, (
            f"expected {_CLONE_COUNT} child games; got {len(created.game_ids)}"
        )

        agent_builds_by_seat = {
            f"P{i + 1:02d}": agent_build_id for i in range(mini7_v1.PLAYER_COUNT)
        }

        all_llm_calls: list[AdapterResult] = []
        terminal_winners: list[str] = []
        for index, game_id in enumerate(created.game_ids):
            adapter = _build_adapter(settings)
            game_seed = derive_game_seed(gauntlet_seed, index)
            config = GameConfig(
                game_id=str(game_id),
                game_seed=game_seed,
                ruleset_id=mini7_v1.RULESET_ID,
                timeout_s=float(settings.padrino_llm_timeout_seconds),
            )
            persistence = GamePersistence(
                session_factory=session_factory,
                game_id=game_id,
                agent_builds=agent_builds_by_seat,
                league_id=league_id,
            )
            outcome = await run_game(config, adapter, ranked=True, persistence=persistence)

            # Every game must reach a terminal state.
            winner = outcome.final_state.terminal_result
            assert winner in _TERMINAL_RESULTS, (
                f"game #{index} did not reach a terminal state; "
                f"terminal_result={winner!r} reason={outcome.final_state.terminal_reason!r}"
            )
            assert outcome.event_log.events, f"game #{index} produced an empty event log"
            assert outcome.event_log.events[-1].body["event_type"] == "GameTerminated", (
                f"game #{index} did not end with GameTerminated"
            )
            terminal_winners.append(winner)
            all_llm_calls.extend(outcome.llm_calls)

        # Parse-rate gate carried forward from the post-Wave-2 audit:
        # silent NOOP / ABSTAIN coercion masked 100% failure rates before
        # commit 4e4ed22 landed; an integration test that doesn't measure
        # parse rate has no way to detect the recurrence.
        assert all_llm_calls, "expected at least one adapter call across the gauntlet"
        for index, call in enumerate(all_llm_calls):
            assert call.raw_response or call.status in _FAILURE_STATUSES, (
                f"call #{index} must have a non-empty raw_response or a failure-indicating "
                f"status; got status={call.status!r} raw_len={len(call.raw_response)}"
            )
        parsed_ok_count = sum(1 for c in all_llm_calls if c.status in _PARSED_OK_STATUSES)
        parse_rate = parsed_ok_count / len(all_llm_calls)
        assert parse_rate >= _PARSE_RATE_GATE, (
            f"only {parsed_ok_count}/{len(all_llm_calls)} ({parse_rate:.0%}) provider "
            f"responses parsed across the gauntlet — the rest fell through to "
            f"safe-fallback coercion. Status histogram: "
            f"{sorted({c.status for c in all_llm_calls})}"
        )

        # Finalize the gauntlet now that every child game is terminal.
        async with session_factory() as session:
            finalized = await finalize_gauntlet_if_done(session, created.gauntlet_id)
        assert finalized is not None, (
            "finalize_gauntlet_if_done returned None — every child game should be terminal"
        )
        assert finalized.status == "COMPLETED"
        assert finalized.diagnostics.games_completed == _CLONE_COUNT

        async with session_factory() as session:
            gauntlet_row = await session.get(Gauntlet, created.gauntlet_id)
            assert gauntlet_row is not None
            assert gauntlet_row.status == "COMPLETED", (
                f"Gauntlet.status did not flip to COMPLETED; got {gauntlet_row.status!r}"
            )
            assert gauntlet_row.completed_at is not None

            # Rating assertions: every scope row must exist with non-default
            # mu / sigma, and faction-stratified rows must be populated for
            # both TOWN and MAFIA.
            rating_rows = list(
                (
                    await session.execute(
                        select(Rating).where(
                            Rating.league_id == league_id,
                            Rating.agent_build_id == agent_build_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rating_rows, (
                "expected at least one rating row for the participating agent_build "
                "after the gauntlet — none were written"
            )
            scope_keys = {(r.scope_type, r.scope_value) for r in rating_rows}
            assert (SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL) in scope_keys, (
                f"GLOBAL/global rating row missing; saw scopes={sorted(scope_keys)}"
            )
            assert (SCOPE_FACTION, "TOWN") in scope_keys, (
                f"FACTION/TOWN rating row missing; saw scopes={sorted(scope_keys)}"
            )
            assert (SCOPE_FACTION, "MAFIA") in scope_keys, (
                f"FACTION/MAFIA rating row missing; saw scopes={sorted(scope_keys)}"
            )
            for row in rating_rows:
                assert row.games > 0, (
                    f"rating {row.scope_type}/{row.scope_value} did not record any "
                    f"games — games={row.games}"
                )
                drifted = (row.mu != INITIAL_MU) or (row.sigma != INITIAL_SIGMA)
                assert drifted, (
                    f"rating {row.scope_type}/{row.scope_value} did not move off "
                    f"OpenSkill defaults — mu={row.mu} sigma={row.sigma}"
                )

            board = await compute_leaderboard(
                session, league_id=league_id, ruleset_id=mini7_v1.RULESET_ID
            )

        # Leaderboard must surface at least one non-empty entry and that entry
        # must be flagged provisional (3 games << the US-045 thresholds of
        # 30 / 15 / 5).
        assert board.entries, "compute_leaderboard returned no entries"
        entry = board.entries[0]
        assert entry.games >= _CLONE_COUNT, (
            f"leaderboard entry recorded {entry.games} games; expected >= {_CLONE_COUNT}"
        )
        assert entry.provisional, (
            f"leaderboard entry should be provisional after {_CLONE_COUNT} games "
            f"(US-045 thresholds: total>=30, town>=15, mafia>=5); got "
            f"provisional={entry.provisional} games={entry.games}"
        )

        total_cost = sum((call.cost_usd or 0.0) for call in all_llm_calls)
        with capsys.disabled():
            print(
                f"\n[US-073] mini-gauntlet run: "
                f"games={_CLONE_COUNT} winners={terminal_winners} "
                f"calls={len(all_llm_calls)} parse_rate={parse_rate:.0%} "
                f"cost=${total_cost:.4f} (cap=${_COST_CAP_USD:.2f})"
            )
        assert total_cost <= _COST_CAP_USD, (
            f"total cost ${total_cost:.4f} exceeded ${_COST_CAP_USD:.2f} cap across "
            f"{len(all_llm_calls)} adapter calls"
        )
    finally:
        await engine.dispose()
