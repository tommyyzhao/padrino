"""Persistence helpers for hash-addressed game-rules snapshots."""

from __future__ import annotations

import hashlib
import string
from collections.abc import Mapping
from typing import Any, Final, cast

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.rulesets import Ruleset, get_ruleset
from padrino.core.rulesets.snapshot import (
    GameRulesSnapshot,
    assert_ruleset_id_matches_snapshot,
    canonical_rules_dict,
    snapshot_from_ruleset,
)

RulesSnapshotJson = dict[str, Any]

_HEX_CHARS: Final[frozenset[str]] = frozenset(string.hexdigits.lower())
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "auth_secret_ref",
        "balance",
        "created_at",
        "credential",
        "funding",
        "identity_mode",
        "principal_id",
        "provider",
        "public_player_id",
        "routing",
        "timestamp",
        "updated_at",
    }
)


class RulesSnapshotValidationError(ValueError):
    """A persisted rules snapshot body/hash pair is invalid."""


def snapshot_pair_for_ruleset(ruleset_id: str) -> tuple[RulesSnapshotJson | None, str | None]:
    """Return the canonical persisted snapshot pair for a registered ruleset.

    Unknown ruleset ids are left nullable for legacy rows and for future custom
    stories that will pass an archived snapshot explicitly.
    """
    try:
        ruleset = get_ruleset(ruleset_id)
    except ValueError:
        return None, None
    body = canonical_rules_dict(snapshot_from_ruleset(ruleset))
    return body, _hash_snapshot_body(body)


def resolve_rules_snapshot_pair(
    *,
    ruleset_id: str,
    rules_snapshot_json: Mapping[str, Any] | None,
    rules_snapshot_hash: str | None,
) -> tuple[RulesSnapshotJson | None, str | None]:
    """Validate or derive the rules snapshot pair for a new row."""
    if rules_snapshot_json is None and rules_snapshot_hash is None:
        return snapshot_pair_for_ruleset(ruleset_id)
    return validate_rules_snapshot_pair(
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )


def validate_rules_snapshot_pair(
    *,
    rules_snapshot_json: Mapping[str, Any] | None,
    rules_snapshot_hash: str | None,
) -> tuple[RulesSnapshotJson | None, str | None]:
    """Return a normalized snapshot pair or raise a stable validation error."""
    if (rules_snapshot_json is None) != (rules_snapshot_hash is None):
        raise RulesSnapshotValidationError("rules_snapshot_pair_required")
    if rules_snapshot_json is None or rules_snapshot_hash is None:
        return None, None

    if len(rules_snapshot_hash) != 64 or any(
        char not in _HEX_CHARS for char in rules_snapshot_hash
    ):
        raise RulesSnapshotValidationError("rules_snapshot_hash_invalid")

    body = dict(rules_snapshot_json)
    forbidden = _forbidden_snapshot_keys(body)
    if forbidden:
        raise RulesSnapshotValidationError(f"rules_snapshot_forbidden_key:{sorted(forbidden)[0]}")

    actual_hash = _hash_snapshot_body(body)
    if actual_hash != rules_snapshot_hash:
        raise RulesSnapshotValidationError("rules_snapshot_hash_mismatch")
    return body, rules_snapshot_hash


def runtime_rules_snapshot_from_pair(
    *,
    ruleset_id: str,
    rules_snapshot_json: Mapping[str, Any] | None,
    rules_snapshot_hash: str | None,
) -> GameRulesSnapshot | None:
    """Rebuild a runtime rules snapshot from a persisted canonical pair."""
    body, snapshot_hash = validate_rules_snapshot_pair(
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )
    if body is None or snapshot_hash is None:
        return None

    registered_ruleset = _registered_ruleset(ruleset_id)
    if registered_ruleset is not None:
        snapshot = snapshot_from_ruleset(registered_ruleset)
        try:
            assert_ruleset_id_matches_snapshot(
                ruleset_id=ruleset_id,
                snapshot=snapshot,
                rules_snapshot_hash=snapshot_hash,
            )
        except ValueError as exc:
            raise RulesSnapshotValidationError(str(exc)) from exc
        return snapshot

    payload = dict(body)
    snapshot = GameRulesSnapshot.model_validate(payload)
    try:
        assert_ruleset_id_matches_snapshot(
            ruleset_id=ruleset_id,
            snapshot=snapshot,
            rules_snapshot_hash=snapshot_hash,
        )
    except ValueError as exc:
        raise RulesSnapshotValidationError(str(exc)) from exc
    return snapshot


def runtime_ruleset_from_pair(
    *,
    ruleset_id: str,
    rules_snapshot_json: Mapping[str, Any] | None,
    rules_snapshot_hash: str | None,
) -> Ruleset:
    """Resolve the runtime ruleset from an archived snapshot or legacy registry id."""
    snapshot = runtime_rules_snapshot_from_pair(
        ruleset_id=ruleset_id,
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )
    if snapshot is not None:
        return cast(Ruleset, snapshot)
    return get_ruleset(ruleset_id)


def ensure_lobby_rules_snapshot_mutable(status: str) -> None:
    """Reject rules snapshot changes once a lobby has launched."""
    if status == "LAUNCHED":
        raise RulesSnapshotValidationError("rules_snapshot_immutable_after_launch")


def _hash_snapshot_body(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(body)).hexdigest()


def _registered_ruleset(ruleset_id: str) -> Ruleset | None:
    try:
        return get_ruleset(ruleset_id)
    except ValueError:
        return None


def _forbidden_snapshot_keys(value: object) -> frozenset[str]:
    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key) in _FORBIDDEN_KEYS}
        for child in value.values():
            found.update(_forbidden_snapshot_keys(child))
        return frozenset(found)
    if isinstance(value, list | tuple):
        list_found: set[str] = set()
        for child in value:
            list_found.update(_forbidden_snapshot_keys(child))
        return frozenset(list_found)
    return frozenset()


__all__ = [
    "RulesSnapshotJson",
    "RulesSnapshotValidationError",
    "ensure_lobby_rules_snapshot_mutable",
    "resolve_rules_snapshot_pair",
    "runtime_rules_snapshot_from_pair",
    "runtime_ruleset_from_pair",
    "snapshot_pair_for_ruleset",
    "validate_rules_snapshot_pair",
]
