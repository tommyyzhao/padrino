"""Tests for the canonical JSON encoder used in hash-chain inputs."""

from __future__ import annotations

import datetime as dt

import pytest

from padrino.core.engine.canonical_json import canonical_dumps


def test_returns_bytes() -> None:
    assert isinstance(canonical_dumps({"a": 1}), bytes)


def test_key_ordering_stable_across_insertion_order() -> None:
    a = {"b": 1, "a": 2, "c": 3}
    b = {"c": 3, "a": 2, "b": 1}
    assert canonical_dumps(a) == canonical_dumps(b)
    assert canonical_dumps(a) == b'{"a":2,"b":1,"c":3}'


def test_nested_dicts_and_lists_sorted_recursively() -> None:
    obj = {
        "outer_b": [{"z": 1, "a": 2}, {"y": 3, "b": 4}],
        "outer_a": {"nested_b": 1, "nested_a": 2},
    }
    expected = b'{"outer_a":{"nested_a":2,"nested_b":1},"outer_b":[{"a":2,"z":1},{"b":4,"y":3}]}'
    assert canonical_dumps(obj) == expected


def test_unicode_strings_preserved_not_escaped() -> None:
    out = canonical_dumps({"name": "café — 日本語"})
    assert "café — 日本語".encode() in out
    assert b"\\u" not in out


def test_no_insignificant_whitespace() -> None:
    out = canonical_dumps({"a": 1, "b": [1, 2, 3]})
    assert b" " not in out
    assert b"\n" not in out
    assert b"\t" not in out


def test_float_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"x": 1.5})


def test_nested_float_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"x": [1, 2, 3.0]})


def test_bytes_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"x": b"hello"})


def test_datetime_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"ts": dt.datetime(2026, 1, 1, tzinfo=dt.UTC)})


def test_date_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"d": dt.date(2026, 1, 1)})


def test_identical_inputs_produce_byte_identical_output() -> None:
    obj1 = {"a": 1, "b": [2, 3], "c": {"d": "x"}}
    obj2 = {"c": {"d": "x"}, "b": [2, 3], "a": 1}
    assert canonical_dumps(obj1) == canonical_dumps(obj2)


def test_ints_and_strings_and_bools_and_none() -> None:
    assert canonical_dumps(None) == b"null"
    assert canonical_dumps(True) == b"true"
    assert canonical_dumps(False) == b"false"
    assert canonical_dumps(42) == b"42"
    assert canonical_dumps(-7) == b"-7"
    assert canonical_dumps("hi") == b'"hi"'


def test_empty_containers() -> None:
    assert canonical_dumps({}) == b"{}"
    assert canonical_dumps([]) == b"[]"


def test_non_string_dict_keys_raise() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({1: "x"})


def test_set_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonical_dumps({"x": {1, 2, 3}})


def test_tuple_serializes_as_list() -> None:
    assert canonical_dumps({"x": (1, 2, 3)}) == b'{"x":[1,2,3]}'


def test_large_integers_preserved() -> None:
    big = 10**40
    assert canonical_dumps({"big": big}) == f'{{"big":{big}}}'.encode()


def test_string_with_control_characters_escaped() -> None:
    out = canonical_dumps({"x": "a\nb"})
    assert out == b'{"x":"a\\nb"}'
