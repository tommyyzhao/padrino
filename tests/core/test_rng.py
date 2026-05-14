"""Tests for the pure SHA-256-based seeded RNG."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from padrino.core.engine.rng import SeededRng


def test_same_seed_produces_same_byte_sequence() -> None:
    a = SeededRng("seed-x")
    b = SeededRng("seed-x")
    assert a.next_bytes(64) == b.next_bytes(64)


def test_different_seeds_produce_different_byte_sequences() -> None:
    a = SeededRng("seed-a").next_bytes(32)
    b = SeededRng("seed-b").next_bytes(32)
    assert a != b


def test_seed_accepts_bytes() -> None:
    str_rng = SeededRng("hello")
    bytes_rng = SeededRng(b"hello")
    assert str_rng.next_bytes(32) == bytes_rng.next_bytes(32)


def test_next_bytes_zero_returns_empty() -> None:
    rng = SeededRng("seed")
    assert rng.next_bytes(0) == b""


def test_next_bytes_negative_raises() -> None:
    rng = SeededRng("seed")
    with pytest.raises(ValueError):
        rng.next_bytes(-1)


def test_next_bytes_is_stateful() -> None:
    rng = SeededRng("seed")
    first = rng.next_bytes(16)
    second = rng.next_bytes(16)
    assert first != second


def test_next_bytes_chunks_match_concatenated_call() -> None:
    a = SeededRng("seed")
    one_shot = a.next_bytes(96)
    b = SeededRng("seed")
    chunks = b.next_bytes(32) + b.next_bytes(32) + b.next_bytes(32)
    assert one_shot == chunks


def test_randbelow_in_range() -> None:
    rng = SeededRng("range-seed")
    for _ in range(1000):
        v = rng.randbelow(7)
        assert 0 <= v < 7


def test_randbelow_one_always_zero() -> None:
    rng = SeededRng("one")
    for _ in range(50):
        assert rng.randbelow(1) == 0


def test_randbelow_zero_raises() -> None:
    rng = SeededRng("seed")
    with pytest.raises(ValueError):
        rng.randbelow(0)


def test_randbelow_negative_raises() -> None:
    rng = SeededRng("seed")
    with pytest.raises(ValueError):
        rng.randbelow(-5)


def test_randbelow_covers_full_range() -> None:
    rng = SeededRng("coverage")
    seen: set[int] = set()
    for _ in range(2000):
        seen.add(rng.randbelow(7))
    assert seen == set(range(7))


def test_randbelow_is_deterministic() -> None:
    a = SeededRng("det")
    b = SeededRng("det")
    seq_a = [a.randbelow(100) for _ in range(50)]
    seq_b = [b.randbelow(100) for _ in range(50)]
    assert seq_a == seq_b


def test_shuffle_preserves_multiset() -> None:
    rng = SeededRng("shuffle")
    items = ["MAFIA", "MAFIA", "DETECTIVE", "DOCTOR", "VILLAGER", "VILLAGER", "VILLAGER"]
    shuffled = rng.shuffle(items)
    assert Counter(shuffled) == Counter(items)
    assert len(shuffled) == len(items)


def test_shuffle_returns_new_list() -> None:
    rng = SeededRng("shuffle-immut")
    items = [1, 2, 3, 4, 5]
    original = list(items)
    shuffled = rng.shuffle(items)
    assert items == original
    assert shuffled is not items


def test_shuffle_is_deterministic() -> None:
    a = SeededRng("shuffle-det")
    b = SeededRng("shuffle-det")
    items = list(range(20))
    assert a.shuffle(items) == b.shuffle(items)


def test_shuffle_empty_list_returns_empty_list() -> None:
    rng = SeededRng("seed")
    assert rng.shuffle([]) == []


def test_shuffle_single_element() -> None:
    rng = SeededRng("seed")
    assert rng.shuffle([42]) == [42]


def test_shuffle_distribution_is_not_identity() -> None:
    rng = SeededRng("nontrivial")
    items = list(range(10))
    shuffled = rng.shuffle(items)
    assert shuffled != items


def test_choice_returns_element_from_sequence() -> None:
    rng = SeededRng("choice")
    items = ("a", "b", "c", "d", "e")
    for _ in range(100):
        assert rng.choice(items) in items


def test_choice_deterministic() -> None:
    a = SeededRng("choice-det")
    b = SeededRng("choice-det")
    items = ("a", "b", "c", "d", "e")
    seq_a = [a.choice(items) for _ in range(20)]
    seq_b = [b.choice(items) for _ in range(20)]
    assert seq_a == seq_b


def test_choice_empty_raises() -> None:
    rng = SeededRng("seed")
    with pytest.raises(IndexError):
        rng.choice([])


def test_rng_module_does_not_import_random() -> None:
    rng_path = (
        Path(__file__).resolve().parents[2] / "src" / "padrino" / "core" / "engine" / "rng.py"
    )
    tree = ast.parse(rng_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"random", "secrets"}
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"random", "secrets"}
