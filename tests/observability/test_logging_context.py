"""Verify that structlog contextvars propagate gauntlet/game/phase/llm_call ids.

The runner, gauntlet scheduler, and LLM dispatcher each bind their own
identifier into structlog's contextvars layer. Downstream events emitted at
deeper layers must inherit every shallower binding via
``structlog.contextvars.merge_contextvars`` so a log consumer can trace a
single LLM call back to its phase, game, gauntlet, and league without
threading IDs through every function signature.

The tests below capture a list of every emitted ``event_dict`` and assert the
correlation IDs are present at the expected layer.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog

from padrino.core.rulesets import mini7_v1
from padrino.llm.mock import NoopMockAdapter
from padrino.observability.events import (
    EVENT_GAME_COMPLETED,
    EVENT_GAME_STARTED,
    EVENT_LLM_CALL_COMPLETED,
    EVENT_LLM_CALL_STARTED,
    EVENT_PHASE_RESOLVED,
    EVENT_PHASE_STARTED,
    EVENT_PRIVACY_AUDIT_COMPLETED,
    EVENT_PRIVACY_AUDIT_LEAK_DETECTED,
)
from padrino.runner.game_runner import GameConfig, run_game

_GAME_SEED = "obs-test-seed-001"


@pytest.fixture()
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Capture every structlog event_dict emitted during the test.

    Reconfigures structlog with a minimal processor chain: merge_contextvars +
    a list-appending capture. Restores the original config on teardown.
    """
    captured: list[dict[str, Any]] = []

    def _capture(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Any:
        captured.append(dict(event_dict))
        raise structlog.DropEvent

    orig = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, _capture],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    try:
        yield captured
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()
        structlog.configure(**orig)


def _events_named(captured: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == name]


async def test_runner_emits_correlated_lifecycle_events(
    captured_logs: list[dict[str, Any]],
) -> None:
    """A full run_game emits game/phase/llm.call/phase.resolved/game.completed."""
    adapter = NoopMockAdapter()
    config = GameConfig(game_id="G-OBS-1", game_seed=_GAME_SEED, timeout_s=1.0)
    outcome = await run_game(config, adapter, ranked=False)
    assert outcome.final_state.terminal_result is not None

    game_started = _events_named(captured_logs, EVENT_GAME_STARTED)
    assert len(game_started) == 1
    assert game_started[0]["game_id"] == "G-OBS-1"
    assert game_started[0]["ruleset_id"] == mini7_v1.RULESET_ID

    phase_started = _events_named(captured_logs, EVENT_PHASE_STARTED)
    assert phase_started, "expected at least one phase.started"
    for entry in phase_started:
        assert entry["game_id"] == "G-OBS-1", entry
        assert "phase_id" in entry
        assert entry["phase_id"]

    phase_resolved = _events_named(captured_logs, EVENT_PHASE_RESOLVED)
    assert len(phase_resolved) == len(phase_started)
    for entry in phase_resolved:
        assert entry["game_id"] == "G-OBS-1"
        assert "phase_id" in entry

    llm_started = _events_named(captured_logs, EVENT_LLM_CALL_STARTED)
    llm_completed = _events_named(captured_logs, EVENT_LLM_CALL_COMPLETED)
    assert llm_started, "expected at least one llm.call.started"
    assert len(llm_started) == len(llm_completed)
    for entry in llm_started:
        assert entry["game_id"] == "G-OBS-1"
        assert "phase_id" in entry
        assert "llm_call_id" in entry
        assert "seat" in entry
    started_call_ids = {e["llm_call_id"] for e in llm_started}
    completed_call_ids = {e["llm_call_id"] for e in llm_completed}
    assert started_call_ids == completed_call_ids
    assert len(started_call_ids) == len(llm_started), "llm_call_id must be unique per call"

    game_completed = _events_named(captured_logs, EVENT_GAME_COMPLETED)
    assert len(game_completed) == 1
    assert game_completed[0]["game_id"] == "G-OBS-1"
    assert game_completed[0]["winner"] in {"TOWN", "MAFIA", "DRAW"}

    # US-078: every game emits a privacy.audit.completed event with the
    # offline auditor's finding count. The NoopMockAdapter produces a clean
    # event log, so no leak_detected event is emitted.
    audit_completed = _events_named(captured_logs, EVENT_PRIVACY_AUDIT_COMPLETED)
    assert len(audit_completed) == 1
    assert audit_completed[0]["game_id"] == "G-OBS-1"
    assert audit_completed[0]["finding_count"] == 0
    assert _events_named(captured_logs, EVENT_PRIVACY_AUDIT_LEAK_DETECTED) == []


async def test_runner_clears_contextvars_on_completion(
    captured_logs: list[dict[str, Any]],
) -> None:
    """game_id and phase_id must NOT leak out of run_game()."""
    adapter = NoopMockAdapter()
    config = GameConfig(game_id="G-LEAK-1", game_seed=_GAME_SEED, timeout_s=1.0)
    await run_game(config, adapter, ranked=False)

    merged = structlog.contextvars.get_contextvars()
    assert "game_id" not in merged
    assert "phase_id" not in merged
    assert "ruleset_id" not in merged


async def test_outer_gauntlet_id_propagates_into_game_events(
    captured_logs: list[dict[str, Any]],
) -> None:
    """A gauntlet_id bound at the caller level appears on every nested event."""
    adapter = NoopMockAdapter()
    config = GameConfig(game_id="G-OBS-3", game_seed=_GAME_SEED, timeout_s=1.0)

    tokens = structlog.contextvars.bind_contextvars(gauntlet_id="GAU-OBS-1")
    try:
        await run_game(config, adapter, ranked=False)
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    for name in (
        EVENT_GAME_STARTED,
        EVENT_PHASE_STARTED,
        EVENT_LLM_CALL_STARTED,
        EVENT_LLM_CALL_COMPLETED,
        EVENT_PHASE_RESOLVED,
        EVENT_GAME_COMPLETED,
    ):
        events = _events_named(captured_logs, name)
        assert events, f"expected {name} events"
        for entry in events:
            assert entry["gauntlet_id"] == "GAU-OBS-1", (name, entry)
