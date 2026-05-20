"""Real-provider smoke test for a single mini7_v1 game day.

This test is marked ``@pytest.mark.integration`` and is therefore *skipped* by
the default test run (``uv run pytest -m "not integration"``). It is intended
to be invoked explicitly with ``uv run pytest -m integration`` against the
real Cerebras (primary) and DeepInfra (fallback) providers.

The test runs a single Day-1 game (one discussion round, one vote, one night
phase) by registering a one-off ruleset stub ``mini7_v1_day1_test`` with
``MAX_DAYS=1`` and ``DISCUSSION_ROUNDS_PER_DAY=1`` into the runner's ruleset
registry. All seven seats are backed by the same :class:`LiteLlmAdapter`
instance — each seat receives an independent observation, so deterministic
identity collisions are not a concern.

Assertions:

* every recorded :class:`AdapterResult` has a non-empty ``raw_response`` *or*
  a status indicating fallback / failure was archived,
* the in-memory event log's hash chain replays cleanly end-to-end,
* total ``cost_usd`` across every adapter call stays under the $0.10 sanity
  guardrail.

If ``CEREBRAS_API_KEY`` is not set in the environment the test is skipped.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv

from padrino.core.engine.replay import replay_event_log
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterStatus, AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.runner import game_runner
from padrino.runner.game_runner import GameConfig, run_game
from padrino.settings import Settings

_DAY1_RULESET_ID = "mini7_v1_day1_test"
# Cap sized for ~25 calls at ~$0.005 each with valid-JSON responses
# (longer completions than the truncated fallback path). Bumped from
# $0.10 after the markdown-fence-stripping fix unblocked full-length
# completions.
_COST_CAP_USD = 0.20
_FAILURE_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"provider_error", "primary_failed", "both_failed", "fallback_ok"}
)


def _day1_ruleset_stub() -> SimpleNamespace:
    """Return a mini7_v1 clone with MAX_DAYS=1 / DISCUSSION_ROUNDS_PER_DAY=1.

    A ``SimpleNamespace`` satisfies every ``Ruleset`` Protocol in the engine
    (phases, observations, win conditions) structurally without needing to
    define a real module.
    """
    return SimpleNamespace(
        RULESET_ID=_DAY1_RULESET_ID,
        PLAYER_COUNT=mini7_v1.PLAYER_COUNT,
        ROLE_COUNTS=mini7_v1.ROLE_COUNTS,
        ROLE_FACTIONS=mini7_v1.ROLE_FACTIONS,
        DISCUSSION_ROUNDS_PER_DAY=1,
        MAX_DAYS=1,
        PUBLIC_MESSAGE_MAX_CHARS=mini7_v1.PUBLIC_MESSAGE_MAX_CHARS,
        PRIVATE_MESSAGE_MAX_CHARS=mini7_v1.PRIVATE_MESSAGE_MAX_CHARS,
        MEMORY_UPDATE_MAX_CHARS=mini7_v1.MEMORY_UPDATE_MAX_CHARS,
        PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT=mini7_v1.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT,
        LLM_TIMEOUT_SECONDS=mini7_v1.LLM_TIMEOUT_SECONDS,
        TEMPERATURE=mini7_v1.TEMPERATURE,
        TOP_P=mini7_v1.TOP_P,
    )


@pytest.mark.integration
async def test_real_providers_one_game_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Day 1 of a mini7_v1 game against real Cerebras + DeepInfra providers."""
    load_dotenv(override=False)
    if not os.environ.get("CEREBRAS_API_KEY"):
        pytest.skip("CEREBRAS_API_KEY not set; skipping real-provider integration test")

    settings = Settings()
    routing = RoutingPolicy(
        primary_model=settings.padrino_primary_model,
        fallback_model=settings.padrino_fallback_model,
    )
    build = AgentBuild(
        provider="cerebras",
        model_id=settings.padrino_primary_model,
        prompt_version="default",
        inference_params={
            "temperature": settings.padrino_temperature,
            "top_p": settings.padrino_top_p,
        },
        adapter_version="litellm-v1",
    )
    adapter = LiteLlmAdapter(
        routing_policy=routing,
        agent_build=build,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
        auth_secret_ref="env:CEREBRAS_API_KEY",
    )

    stub = _day1_ruleset_stub()
    monkeypatch.setitem(game_runner._RULESETS, _DAY1_RULESET_ID, stub)

    config = GameConfig(
        game_id="G-INTEGRATION-REAL-001",
        game_seed="integration-real-day1-001",
        ruleset_id=_DAY1_RULESET_ID,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
    )

    outcome = await run_game(config, adapter, ranked=False)

    assert outcome.llm_calls, "expected at least one adapter call during Day 1"
    for index, call in enumerate(outcome.llm_calls):
        assert call.raw_response or call.status in _FAILURE_STATUSES, (
            f"call #{index} must have a non-empty raw_response or a failure-indicating "
            f"status; got status={call.status!r} raw_len={len(call.raw_response)}"
        )

    # Real gameplay quality gate: at least 70% of provider responses must parse
    # cleanly into a valid AgentResponse. Falling below this threshold means
    # the runner is silently coercing most calls to safe-fallback (NOOP /
    # ABSTAIN) and the gauntlet stops measuring model behavior. The bug this
    # gate prevents was a markdown ```json ... ``` wrap that caused 100% of
    # Cerebras responses to fail parse_agent_response while the rest of this
    # test still passed.
    statuses = [call.status for call in outcome.llm_calls]
    ok_count = sum(1 for s in statuses if s == "ok")
    invalid_json_count = sum(1 for s in statuses if s == "invalid_json")
    parse_rate = ok_count / len(outcome.llm_calls)
    assert parse_rate >= 0.7, (
        f"only {ok_count}/{len(outcome.llm_calls)} ({parse_rate:.0%}) provider "
        f"responses parsed as valid AgentResponse — the rest fell through to "
        f"safe-fallback coercion. invalid_json={invalid_json_count}. "
        f"Status histogram: {sorted(set(statuses))}"
    )

    events = outcome.event_log.events
    replayed = replay_event_log(events)
    assert len(replayed.events) == len(events)
    for original, repeated in zip(events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.prev_event_hash == repeated.prev_event_hash
        assert original.sequence == repeated.sequence

    total_cost = sum((call.cost_usd or 0.0) for call in outcome.llm_calls)
    assert total_cost <= _COST_CAP_USD, (
        f"total cost ${total_cost:.4f} exceeded ${_COST_CAP_USD:.2f} cap across "
        f"{len(outcome.llm_calls)} adapter calls"
    )
