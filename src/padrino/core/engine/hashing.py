"""Hash-chain helpers for the deterministic event log.

Each event's `event_hash` is `sha256(prev_event_hash + canonical_json(body))`,
where `body` excludes the `event_hash`, `prev_event_hash`, and `created_at`
fields so the hash covers only intrinsic event content. The first event in a
chain uses `GENESIS_HASH`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Final

from padrino.core.engine.canonical_json import canonical_dumps

GENESIS_HASH: Final[str] = "0" * 64

_EXCLUDED_KEYS: Final[frozenset[str]] = frozenset({"event_hash", "prev_event_hash", "created_at"})


def compute_event_hash(prev_event_hash: str, event_body: Mapping[str, Any]) -> str:
    """Return the hex SHA-256 digest binding `event_body` to `prev_event_hash`.

    Keys `event_hash`, `prev_event_hash`, and `created_at` are stripped from
    `event_body` before hashing so server timestamps and chain pointers do not
    perturb intrinsic event content. The input mapping is not mutated.
    """
    filtered = {k: v for k, v in event_body.items() if k not in _EXCLUDED_KEYS}
    payload = prev_event_hash.encode("utf-8") + canonical_dumps(filtered)
    return hashlib.sha256(payload).hexdigest()
