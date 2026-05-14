"""Tests for the in-memory hash-chained EventLog."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash


def _body(seq_marker: int) -> dict[str, Any]:
    return {
        "event_type": "PhaseStarted",
        "phase": "DAY_1_DISCUSSION",
        "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": seq_marker},
    }


def test_empty_log_head_hash_is_genesis() -> None:
    log = EventLog()
    assert log.head_hash == GENESIS_HASH
    assert log.events == ()


def test_first_append_links_to_genesis() -> None:
    log = EventLog()
    stored = log.append(_body(1))
    assert stored.sequence == 0
    assert stored.prev_event_hash == GENESIS_HASH
    assert stored.event_hash == compute_event_hash(GENESIS_HASH, _body(1))


def test_append_returns_stored_event_type() -> None:
    log = EventLog()
    stored = log.append(_body(1))
    assert isinstance(stored, StoredEvent)


def test_sequences_are_contiguous_from_zero() -> None:
    log = EventLog()
    for i in range(5):
        log.append(_body(i))
    seqs = [e.sequence for e in log.events]
    assert seqs == [0, 1, 2, 3, 4]


def test_head_hash_matches_last_event() -> None:
    log = EventLog()
    log.append(_body(1))
    last = log.append(_body(2))
    assert log.head_hash == last.event_hash


def test_chain_links_prev_to_previous_hash() -> None:
    log = EventLog()
    log.append(_body(1))
    log.append(_body(2))
    log.append(_body(3))
    events = log.events
    for i in range(1, len(events)):
        assert events[i].prev_event_hash == events[i - 1].event_hash


def test_chain_validates_via_recomputation() -> None:
    log = EventLog()
    for i in range(4):
        log.append(_body(i))
    prev = GENESIS_HASH
    for stored in log.events:
        assert stored.prev_event_hash == prev
        assert stored.event_hash == compute_event_hash(prev, stored.body)
        prev = stored.event_hash


def test_tampering_invalidates_downstream_hashes() -> None:
    log = EventLog()
    for i in range(3):
        log.append(_body(i))
    events = log.events
    # Tamper with event #1's body and recompute the chain forward.
    tampered_body = dict(events[1].body)
    tampered_body["payload"] = {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 999}
    recomputed = compute_event_hash(events[0].event_hash, tampered_body)
    # The stored event_hash for event #1 no longer matches the tampered body.
    assert recomputed != events[1].event_hash
    # And cascading forward, event #2's prev would no longer link.
    assert events[2].prev_event_hash != recomputed


def test_created_at_excluded_from_hash() -> None:
    body_a: dict[str, Any] = {"event_type": "X", "phase": "P", "created_at": "2026-01-01T00:00:00Z"}
    body_b: dict[str, Any] = {"event_type": "X", "phase": "P", "created_at": "2099-12-31T23:59:59Z"}
    log_a = EventLog()
    log_b = EventLog()
    a = log_a.append(body_a)
    b = log_b.append(body_b)
    assert a.event_hash == b.event_hash


def test_events_view_is_immutable_tuple() -> None:
    log = EventLog()
    log.append(_body(1))
    snapshot = log.events
    assert isinstance(snapshot, tuple)
    # Mutating the snapshot must not affect the log: tuples are immutable.
    log.append(_body(2))
    # Old snapshot length unchanged.
    assert len(snapshot) == 1
    # New events read includes both.
    assert len(log.events) == 2


def test_stored_event_is_frozen() -> None:
    log = EventLog()
    stored = log.append(_body(1))
    with pytest.raises(ValidationError):
        stored.sequence = 99  # type: ignore[misc]


def test_caller_mutation_after_append_does_not_affect_chain() -> None:
    log = EventLog()
    body = _body(1)
    stored = log.append(body)
    # Mutating the body the caller handed us after the fact must not move the hash.
    body["payload"] = {"hacked": True}
    assert stored.event_hash == compute_event_hash(GENESIS_HASH, _body(1))


def test_no_module_imports_db_or_clock() -> None:
    import ast
    from pathlib import Path

    src = Path("src/padrino/core/engine/event_log.py").read_text()
    tree = ast.parse(src)
    forbidden = {"padrino.db", "padrino.llm", "padrino.api", "padrino.runner", "time", "datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"forbidden from-import: {node.module}"
