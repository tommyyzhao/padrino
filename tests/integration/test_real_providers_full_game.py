"""Full 5-day real-provider integration test (US-071).

Drives the unmodified ``mini7_v1`` ruleset (``MAX_DAYS=5``,
``DISCUSSION_ROUNDS_PER_DAY=3``) through :func:`run_game` against the real
Cerebras (primary) + DeepInfra (fallback) providers, then asserts the game
actually resolved to a TOWN or MAFIA win (not a MAX_DAYS DRAW), the hash
chain replays clean, and the parse rate stayed above the post-Wave-2
``>=70%`` quality gate.

The test is marked ``@pytest.mark.integration`` and skipped on missing
``CEREBRAS_API_KEY`` so default CI (``-m "not integration"``) never hits a
real provider. The live-LLM CI job is opt-in via a separate workflow gated
on a repo secret.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from padrino.core.engine.replay import replay_event_log
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterStatus, AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.observability.privacy_audit import audit_ranked_observations
from padrino.runner.game_runner import GameConfig, run_game
from padrino.settings import Settings

# Sized for ~7 seats * 5 days * ~10 phases * $0.005/call at realistic
# completion lengths. The post-Wave-2 Day-1 cap was $0.20 for ~25 calls;
# the full game is bounded by 7 seats * 3 discussion rounds * 5 days for
# the day phases plus night phases, so a ~10x bump is conservative.
_COST_CAP_USD = 2.00
_PARSE_RATE_GATE = 0.70
# A "parsed-OK" call is one where the runner received a valid AgentResponse
# from ANY host: primary directly (`ok`), or the configured fallback host
# after the primary errored (`fallback_ok`). Both are real gameplay; only
# the failure / coercion paths are excluded. Counting `ok` alone would
# treat a healthy fallback path as a parse failure.
_PARSED_OK_STATUSES: frozenset[AdapterStatus] = frozenset({"ok", "fallback_ok"})
_FAILURE_STATUSES: frozenset[AdapterStatus] = frozenset(
    {"provider_error", "primary_failed", "both_failed", "fallback_ok"}
)

# Submission event types that represent a real action (not NOOP / not
# ABSTAIN). NOOP never emits a submission event in the runner; ABSTAIN
# emits a VoteSubmitted with ``payload.is_abstain=True``.
_ACTION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "MafiaKillVoteSubmitted",
        "ProtectSubmitted",
        "InvestigateSubmitted",
    }
)


@pytest.mark.integration
async def test_real_providers_full_game(capsys: pytest.CaptureFixture[str]) -> None:
    """Run a full mini7_v1 game against real providers; assert it terminates with a winner.

    Real LLMs are non-deterministic, so the test does NOT assert a specific
    winner — only that the engine, adapters, retry policy, ratings path, and
    event log all survive a complete live run and reach a non-DRAW terminal.
    Provider variance can occasionally produce a MAX_DAYS DRAW; treat that
    as a real flake (re-run once before filing) rather than a code bug.
    """
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

    config = GameConfig(
        game_id="G-INTEGRATION-REAL-FULL-001",
        game_seed="integration-real-full-001",
        ruleset_id=mini7_v1.RULESET_ID,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
    )

    outcome = await run_game(config, adapter, ranked=False)

    # (a) GameTerminated is the final event in the log.
    events = outcome.event_log.events
    assert events, "expected a non-empty event log"
    final_event = events[-1]
    assert final_event.body["event_type"] == "GameTerminated", (
        f"expected the final event to be GameTerminated; got {final_event.body['event_type']!r}"
    )
    terminated_events = [e for e in events if e.body["event_type"] == "GameTerminated"]
    assert len(terminated_events) == 1, (
        f"expected exactly one GameTerminated event; got {len(terminated_events)}"
    )

    # (b) Winner is TOWN or MAFIA, not DRAW. A DRAW means MAX_DAYS_REACHED,
    # which is acceptable for the Day-1 sanity test but indicates real-LLM
    # gameplay didn't actually resolve here.
    winner = outcome.final_state.terminal_result
    assert winner in {"TOWN", "MAFIA"}, (
        f"expected a TOWN or MAFIA win; got terminal_result={winner!r} "
        f"reason={outcome.final_state.terminal_reason!r}"
    )

    # (c) parse-rate >= 70% (post-Wave-2 quality gate carried forward).
    assert outcome.llm_calls, "expected at least one adapter call across the game"
    for index, call in enumerate(outcome.llm_calls):
        assert call.raw_response or call.status in _FAILURE_STATUSES, (
            f"call #{index} must have a non-empty raw_response or a failure-indicating "
            f"status; got status={call.status!r} raw_len={len(call.raw_response)}"
        )
    statuses = [call.status for call in outcome.llm_calls]
    parsed_ok_count = sum(1 for s in statuses if s in _PARSED_OK_STATUSES)
    invalid_json_count = sum(1 for s in statuses if s == "invalid_json")
    parse_rate = parsed_ok_count / len(outcome.llm_calls)
    assert parse_rate >= _PARSE_RATE_GATE, (
        f"only {parsed_ok_count}/{len(outcome.llm_calls)} ({parse_rate:.0%}) "
        f"provider responses parsed as valid AgentResponse — the rest fell "
        f"through to safe-fallback coercion. invalid_json={invalid_json_count}. "
        f"Status histogram: {sorted(set(statuses))}"
    )

    # (d) Hash-chain replays bit-for-bit clean across every event.
    replayed = replay_event_log(events)
    assert len(replayed.events) == len(events)
    for original, repeated in zip(events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash, (
            f"hash mismatch at sequence={original.sequence}: "
            f"{original.event_hash!r} vs {repeated.event_hash!r}"
        )
        assert original.prev_event_hash == repeated.prev_event_hash
        assert original.sequence == repeated.sequence

    # (e) A meaningful majority of seats produces a non-NOOP / non-ABSTAIN
    # action somewhere in the log. NOOP emits no submission event; ABSTAIN
    # emits VoteSubmitted with payload.is_abstain=True. A real game action
    # is any of the structured action events OR a VoteSubmitted with
    # is_abstain=False. The original criterion ("every seat must produce
    # one") is unachievable in a legitimate game: a villager who ABSTAINs
    # on Day 1 and dies Night 1 has no other chance, since Day Discussion
    # forces NOOP and villagers/goons cannot inspect/protect/kill. The
    # threshold here is 5 of 7 seats — robust to up to two early-game
    # ABSTAIN+kill combinations while still catching silent NOOP coercion
    # (which would produce zero real actions across all seats).
    real_action_actors: set[str] = set()
    for event in events:
        body = event.body
        event_type = body["event_type"]
        actor = body.get("actor_player_id")
        if actor is None:
            continue
        if event_type in _ACTION_EVENT_TYPES:
            real_action_actors.add(actor)
        elif event_type == "VoteSubmitted":
            payload = body.get("payload", {})
            if not payload.get("is_abstain", False):
                real_action_actors.add(actor)
    seat_ids = {seat.public_player_id for seat in outcome.final_state.seats}
    real_action_seat_count = len(seat_ids & real_action_actors)
    min_real_action_seats = max(1, len(seat_ids) - 2)
    assert real_action_seat_count >= min_real_action_seats, (
        f"only {real_action_seat_count}/{len(seat_ids)} seats produced a "
        f"non-NOOP / non-ABSTAIN action across the game; expected at least "
        f"{min_real_action_seats}. Seats with real actions: "
        f"{sorted(seat_ids & real_action_actors)}; "
        f"seats without: {sorted(seat_ids - real_action_actors)}"
    )

    # (f) US-078 privacy audit on the realized event log: every per-seat
    # observation must be free of cross-seat role / faction / model-identity /
    # ratings / clone-index leaks. ANY finding fails the test with the field
    # path so the regression is traceable without re-leaking the value.
    audit_report = audit_ranked_observations(outcome.event_log, outcome.seat_assignments)
    assert audit_report.finding_count == 0, (
        "ranked-mode privacy audit found cross-seat leaks: "
        + ", ".join(
            f"{f.field_path}(observed_by={f.seat_observed_by})" for f in audit_report.findings
        )
    )

    # Cost cap: print actual cost on success so the cap can be tuned.
    total_cost = sum((call.cost_usd or 0.0) for call in outcome.llm_calls)
    with capsys.disabled():
        print(
            f"\n[US-071] full-game run: "
            f"winner={winner} reason={outcome.final_state.terminal_reason} "
            f"calls={len(outcome.llm_calls)} parse_rate={parse_rate:.0%} "
            f"cost=${total_cost:.4f} (cap=${_COST_CAP_USD:.2f})"
        )
    assert total_cost <= _COST_CAP_USD, (
        f"total cost ${total_cost:.4f} exceeded ${_COST_CAP_USD:.2f} cap across "
        f"{len(outcome.llm_calls)} adapter calls"
    )
