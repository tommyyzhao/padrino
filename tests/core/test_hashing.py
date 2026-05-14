"""Tests for the hash-chain event hashing function."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash


def test_genesis_hash_is_64_zero_hex_chars() -> None:
    assert GENESIS_HASH == "0" * 64
    assert len(GENESIS_HASH) == 64


def test_returns_64_char_hex_digest() -> None:
    digest = compute_event_hash(GENESIS_HASH, {"kind": "game_started"})
    assert isinstance(digest, str)
    assert len(digest) == 64
    int(digest, 16)  # raises if non-hex


def test_deterministic_for_same_input() -> None:
    body: dict[str, Any] = {"kind": "vote_cast", "voter": "p1", "target": "p2"}
    assert compute_event_hash(GENESIS_HASH, body) == compute_event_hash(GENESIS_HASH, body)


def test_matches_explicit_sha256_construction() -> None:
    body = {"kind": "phase_start", "day": 1}
    expected = hashlib.sha256(GENESIS_HASH.encode("utf-8") + canonical_dumps(body)).hexdigest()
    assert compute_event_hash(GENESIS_HASH, body) == expected


def test_changing_non_excluded_field_changes_hash() -> None:
    a = compute_event_hash(GENESIS_HASH, {"kind": "vote_cast", "target": "p1"})
    b = compute_event_hash(GENESIS_HASH, {"kind": "vote_cast", "target": "p2"})
    assert a != b


def test_changing_prev_hash_changes_hash() -> None:
    body = {"kind": "vote_cast", "target": "p1"}
    a = compute_event_hash(GENESIS_HASH, body)
    b = compute_event_hash("a" * 64, body)
    assert a != b


def test_excludes_event_hash_field_from_input() -> None:
    base = {"kind": "vote_cast", "target": "p1"}
    with_hash = {**base, "event_hash": "deadbeef" * 8}
    assert compute_event_hash(GENESIS_HASH, base) == compute_event_hash(GENESIS_HASH, with_hash)


def test_excludes_prev_event_hash_field_from_input() -> None:
    base = {"kind": "vote_cast", "target": "p1"}
    with_prev = {**base, "prev_event_hash": "feedface" * 8}
    assert compute_event_hash(GENESIS_HASH, base) == compute_event_hash(GENESIS_HASH, with_prev)


def test_excludes_created_at_field_from_input() -> None:
    base = {"kind": "vote_cast", "target": "p1"}
    with_ts = {**base, "created_at": "2026-05-14T00:00:00Z"}
    assert compute_event_hash(GENESIS_HASH, base) == compute_event_hash(GENESIS_HASH, with_ts)


def test_excludes_all_three_fields_simultaneously() -> None:
    base = {"kind": "vote_cast", "target": "p1"}
    bloated = {
        **base,
        "event_hash": "aa" * 32,
        "prev_event_hash": "bb" * 32,
        "created_at": "2026-05-14T00:00:00Z",
    }
    assert compute_event_hash(GENESIS_HASH, base) == compute_event_hash(GENESIS_HASH, bloated)


def test_does_not_mutate_input_mapping() -> None:
    body = {
        "kind": "vote_cast",
        "event_hash": "x" * 64,
        "prev_event_hash": "y" * 64,
        "created_at": "2026-05-14",
    }
    snapshot = dict(body)
    compute_event_hash(GENESIS_HASH, body)
    assert body == snapshot


def test_chain_of_three_events_validates_when_prev_matches() -> None:
    event1_body = {"kind": "game_started", "seed": "abc"}
    h1 = compute_event_hash(GENESIS_HASH, event1_body)

    event2_body = {"kind": "phase_start", "day": 1}
    h2 = compute_event_hash(h1, event2_body)

    event3_body = {"kind": "vote_cast", "voter": "p1", "target": "p2"}
    h3 = compute_event_hash(h2, event3_body)

    # Re-verify the chain end-to-end using the documented recurrence.
    assert compute_event_hash(GENESIS_HASH, event1_body) == h1
    assert compute_event_hash(h1, event2_body) == h2
    assert compute_event_hash(h2, event3_body) == h3
    # Each hash is distinct.
    assert len({h1, h2, h3}) == 3


def test_chain_breaks_if_predecessor_hash_is_wrong() -> None:
    body = {"kind": "phase_start", "day": 1}
    correct = compute_event_hash(GENESIS_HASH, body)
    tampered = compute_event_hash("f" * 64, body)
    assert correct != tampered


def test_rejects_non_string_keys_via_canonical_dumps() -> None:
    with pytest.raises(TypeError):
        compute_event_hash(GENESIS_HASH, {1: "bad"})  # type: ignore[dict-item]


def test_key_order_in_body_does_not_affect_hash() -> None:
    a = compute_event_hash(GENESIS_HASH, {"a": 1, "b": 2, "c": 3})
    b = compute_event_hash(GENESIS_HASH, {"c": 3, "b": 2, "a": 1})
    assert a == b
