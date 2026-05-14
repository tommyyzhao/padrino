"""Deterministic JSON encoder for hash-chain inputs.

Produces byte-identical output for semantically equal inputs across machines
and Python versions. Floats, bytes, datetimes, and non-string dict keys are
rejected so callers must pre-serialize them to a chosen canonical form.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_dumps(obj: Any) -> bytes:
    """Encode `obj` to canonical UTF-8 JSON bytes.

    Rules:
      - Object keys are sorted lexicographically at every depth.
      - No insignificant whitespace.
      - `ensure_ascii=False` so unicode characters are preserved literally.
      - `float`, `bytes`, `bytearray`, `set`/`frozenset`, `datetime`, `date`,
        and other non-JSON-native types raise `TypeError`.
      - Dict keys must be `str` (not `int`, not `bool`); raises `TypeError`
        otherwise.

    Encode numerics as `int` or `str`. Pre-serialize timestamps to ISO strings.
    """
    _validate(obj)
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate(obj: Any) -> None:
    if obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, str):
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _validate(item)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str) or isinstance(key, bool):
                raise TypeError(
                    f"canonical_dumps requires string dict keys; got {type(key).__name__}"
                )
            _validate(value)
        return
    raise TypeError(f"canonical_dumps does not support values of type {type(obj).__name__}")
