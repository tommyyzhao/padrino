"""Ranked-mode observation privacy guard.

In ranked gauntlets, the engine must never let an agent see information that
gives it a side-channel advantage over other competitors. This module exposes
:func:`assert_ranked_observation_safe`, a pure check the runner calls on every
:class:`~padrino.core.observations.Observation` immediately before dispatching
to the LLM adapter in ranked mode.

Forbidden information surfaces (per ``prd.md`` §6.1):

* Agent build identifiers (``agent_build_id``).
* Model or provider names (``model_id``, ``model_name``, ``provider``,
  ``provider_name``).
* Ratings (``rating``, ``ratings``, ``elo``, ``openskill_mu``,
  ``openskill_sigma``, bare ``mu`` / ``sigma`` carriers).
* Historical win rates (``win_rate``, ``win_rates``).
* Gauntlet clone index (``gauntlet_clone_index``, ``clone_index``).
* Other games' transcripts (``game_id`` / ``game_public_id`` payload keys whose
  value differs from the observation's own game id).
* Hidden roles for non-self / non-teammate seats (``role`` or ``faction``
  surfacing inside any event payload — the engine never emits these
  legitimately; ``you.role`` / ``mafia_teammates`` carry the *allowed*
  disclosures at the top level).

The check is purely structural: it walks the ``payload`` dict of every event
entry in ``public_events`` and ``private_events`` and scans
``your_private_memory`` for forbidden tokens. It does not reason about the
*semantics* of fields like ``finding`` (detective inspection result), which
legitimately reveal faction-equivalent info to a specific role.

Pure function. No DB / LLM / clock / network access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from padrino.core.observations import Observation


class RankedPrivacyViolation(ValueError):
    """Raised when a ranked observation contains forbidden information."""


class AnonymityViolation(ValueError):
    """Raised when a payload reaching a public/observation surface leaks a
    human-vs-AI or model-identity marker before the endgame reveal."""


class LeakFinding(BaseModel):
    """One privacy-audit finding from :func:`audit_observation_log_for_seat`.

    ``leaked_value_redacted`` carries a short type / shape label (never the
    raw value) so the audit log can be safely shipped to operators without
    re-leaking the very secret the audit is trying to detect.
    """

    model_config = ConfigDict(frozen=True)

    field_path: str
    leaked_value_redacted: str
    seat_observed_by: str
    seat_owning_the_leak: str | None


#: Human-multiplayer identity markers (Wave 9). Any of these reaching an
#: observation / public / spectator surface before the endgame reveal would let
#: a viewer tell which seat is human vs AI, or re-identify the AI behind a
#: takeover. They are folded into :data:`FORBIDDEN_PAYLOAD_KEYS` so every
#: existing deny-list consumer (ranked guard, spectator projection, public
#: transcript, export bundle) inherits them for free.
HUMAN_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "is_human",
        "controller_type",
        "seat_kind",
        "occupant_principal_id",
        "occupant_user_id",
        "human_player_id",
        "takeover",
        "taken_over_at_phase",
        "takeover_agent_build_id",
    }
)

FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            "agent_build_id",
            "model_id",
            "model_name",
            "provider",
            "provider_name",
            "rating",
            "ratings",
            "win_rate",
            "win_rates",
            "elo",
            "openskill_mu",
            "openskill_sigma",
            "mu",
            "sigma",
            "gauntlet_clone_index",
            "clone_index",
            "role",
            "faction",
        }
    )
    | HUMAN_IDENTITY_KEYS
)

FORBIDDEN_MEMORY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "agent_build_id",
        "model_id",
        "gauntlet_clone_index",
        "openskill",
    }
)

GAME_ID_KEYS: Final[frozenset[str]] = frozenset({"game_id", "game_public_id"})


def assert_ranked_observation_safe(obs: Observation) -> None:
    """Raise :class:`RankedPrivacyViolation` if ``obs`` leaks ranked-forbidden info."""
    own_game_id = obs.game_public_id

    for entry in obs.public_events:
        _walk(entry.payload, own_game_id, f"public_events[seq={entry.sequence}].payload")
    for entry in obs.private_events:
        _walk(entry.payload, own_game_id, f"private_events[seq={entry.sequence}].payload")

    _check_text(obs.your_private_memory, "your_private_memory")


def _walk(value: Any, own_game_id: str, path: str) -> None:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            sub_path = f"{path}.{key}"
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise RankedPrivacyViolation(f"forbidden ranked field {key!r} at {sub_path}")
            if key in GAME_ID_KEYS and isinstance(sub_value, str) and sub_value != own_game_id:
                raise RankedPrivacyViolation(
                    f"foreign game reference {key}={sub_value!r} at {sub_path} "
                    f"(observation game is {own_game_id!r})"
                )
            _walk(sub_value, own_game_id, sub_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _walk(item, own_game_id, f"{path}[{index}]")


def _check_text(text: str, path: str) -> None:
    lowered = text.lower()
    for token in FORBIDDEN_MEMORY_TOKENS:
        if token in lowered:
            raise RankedPrivacyViolation(f"forbidden ranked token {token!r} found in {path}")


def audit_observation_log_for_seat(
    observations: Sequence[Observation],
    seat_id: str,
) -> list[LeakFinding]:
    """Audit every observation rendered to ``seat_id`` and return all findings.

    Mirrors :func:`assert_ranked_observation_safe` semantically but collects
    every violation instead of raising on the first. The two functions share
    :data:`FORBIDDEN_PAYLOAD_KEYS`, :data:`FORBIDDEN_MEMORY_TOKENS`, and
    :data:`GAME_ID_KEYS` so the runtime guard and the offline auditor can
    never drift out of sync.

    Each :class:`LeakFinding` carries a ``field_path`` (the dotted access path
    into the observation), a ``leaked_value_redacted`` shape label (type name
    + length, never the raw value), the seat that received the leak
    (``seat_observed_by`` = ``seat_id``), and — when the leak lives inside an
    event payload — the ``actor_player_id`` of that event as
    ``seat_owning_the_leak`` (``None`` for memory leaks).

    Pure function. Returns an empty list on a clean observation log.
    """
    findings: list[LeakFinding] = []
    for obs in observations:
        own_game_id = obs.game_public_id
        for entry in obs.public_events:
            _collect_payload(
                entry.payload,
                own_game_id,
                f"public_events[seq={entry.sequence}].payload",
                seat_id,
                entry.actor_player_id,
                findings,
            )
        for entry in obs.private_events:
            _collect_payload(
                entry.payload,
                own_game_id,
                f"private_events[seq={entry.sequence}].payload",
                seat_id,
                entry.actor_player_id,
                findings,
            )
        _collect_memory(obs.your_private_memory, seat_id, findings)
    return findings


def _collect_payload(
    value: Any,
    own_game_id: str,
    path: str,
    seat_observed_by: str,
    actor_player_id: str | None,
    findings: list[LeakFinding],
) -> None:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            sub_path = f"{path}.{key}"
            is_forbidden_key = key in FORBIDDEN_PAYLOAD_KEYS
            is_foreign_game_ref = (
                key in GAME_ID_KEYS and isinstance(sub_value, str) and sub_value != own_game_id
            )
            if is_forbidden_key or is_foreign_game_ref:
                findings.append(
                    LeakFinding(
                        field_path=sub_path,
                        leaked_value_redacted=_redact(sub_value),
                        seat_observed_by=seat_observed_by,
                        seat_owning_the_leak=actor_player_id,
                    )
                )
            _collect_payload(
                sub_value, own_game_id, sub_path, seat_observed_by, actor_player_id, findings
            )
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _collect_payload(
                item,
                own_game_id,
                f"{path}[{index}]",
                seat_observed_by,
                actor_player_id,
                findings,
            )


def _collect_memory(text: str, seat_id: str, findings: list[LeakFinding]) -> None:
    lowered = text.lower()
    for token in FORBIDDEN_MEMORY_TOKENS:
        if token in lowered:
            findings.append(
                LeakFinding(
                    field_path="your_private_memory",
                    leaked_value_redacted=f"<str:contains:{token}>",
                    seat_observed_by=seat_id,
                    seat_owning_the_leak=None,
                )
            )


def _redact(value: Any) -> str:
    type_name = type(value).__name__
    if isinstance(value, str | list | tuple | dict | bytes):
        return f"<{type_name}:len={len(value)}>"
    return f"<{type_name}>"


# --------------------------------------------------------------------------- #
# Wave 9: anonymity guard (human-vs-AI / model identity)
# --------------------------------------------------------------------------- #

#: The canonical identity modes. Kept as bare strings here (rather than
#: importing an enum) so the pure guard has no forward dependency on the
#: ``IdentityMode`` enum introduced later in the wave. ``ANONYMOUS`` is the
#: fail-closed default.
ANONYMOUS: Final[str] = "ANONYMOUS"
TRANSPARENT: Final[str] = "TRANSPARENT"


def coerce_identity_mode(mode: Any) -> str:
    """Resolve ``mode`` to a canonical identity mode, FAILING CLOSED.

    A missing / ``None`` / unrecognised mode coerces to :data:`ANONYMOUS`
    (strip), never :data:`TRANSPARENT`. Only an explicit, exact
    ``"TRANSPARENT"`` (case-insensitive, accepting an enum's ``.value`` too)
    opts out of stripping. This is the single chokepoint every surface uses so
    a forgotten / null ``identity_mode`` column can never silently reveal
    identities.
    """
    if mode is None:
        return ANONYMOUS
    raw = getattr(mode, "value", mode)
    if not isinstance(raw, str):
        return ANONYMOUS
    return TRANSPARENT if raw.strip().upper() == TRANSPARENT else ANONYMOUS


def is_anonymous(mode: Any) -> bool:
    """True when ``mode`` resolves (fail-closed) to anonymous stripping."""
    return coerce_identity_mode(mode) == ANONYMOUS


def assert_anonymous_safe(payload: Any) -> None:
    """Raise :class:`AnonymityViolation` if ``payload`` carries a forbidden key.

    Walks nested dicts / lists / tuples and raises on the FIRST
    :data:`FORBIDDEN_PAYLOAD_KEYS` member (which now includes every
    :data:`HUMAN_IDENTITY_KEYS` marker). Pure structural check — it inspects
    keys only, never values, so it never re-leaks the secret it guards.
    """
    if isinstance(payload, Mapping):
        for key, sub_value in payload.items():
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise AnonymityViolation(f"forbidden anonymity key {key!r} in payload")
            assert_anonymous_safe(sub_value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            assert_anonymous_safe(item)


#: Allowlist of seat-row fields that MAY reach a public / observation surface.
#: Anything not listed (notably every :data:`HUMAN_IDENTITY_KEYS` column and a
#: future new identity column) is dropped by :func:`project_seat_row` — so an
#: identity column cannot leak even though it is not a *payload* key.
PUBLIC_SEAT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "public_player_id",
        "seat_index",
        "alive",
        "death_phase",
    }
)

#: Allowlist of game-row fields that MAY reach a public / observation surface
#: before the endgame reveal.
PUBLIC_GAME_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "public_id",
        "ruleset_id",
        "status",
        "broadcast_state",
        "created_at",
        "day",
        "phase",
    }
)


def project_row_through_allowlist(
    row: Mapping[str, Any],
    allowlist: frozenset[str],
    *,
    identity_mode: Any = None,
) -> dict[str, Any]:
    """Project a DB row dict through ``allowlist``, dropping every other field.

    This is the COLUMN-level half of the guard: a new identity column added to
    ``game_seats`` / ``games`` is dropped here even though it is not a payload
    key. In :data:`TRANSPARENT` mode the allowlist is still applied (identity
    surfacing is the caller's job via a wider projection), but the result is
    additionally run through :func:`assert_anonymous_safe` in ANONYMOUS mode so
    a mistakenly-allowlisted forbidden key still fails closed.
    """
    projected = {k: v for k, v in row.items() if k in allowlist}
    if is_anonymous(identity_mode):
        assert_anonymous_safe(projected)
    return projected


def project_seat_row(row: Mapping[str, Any], *, identity_mode: Any = None) -> dict[str, Any]:
    """Project a single seat row for a public / observation surface (fail-closed)."""
    return project_row_through_allowlist(row, PUBLIC_SEAT_FIELDS, identity_mode=identity_mode)


def project_game_row(row: Mapping[str, Any], *, identity_mode: Any = None) -> dict[str, Any]:
    """Project a single game row for a public / observation surface (fail-closed)."""
    return project_row_through_allowlist(row, PUBLIC_GAME_FIELDS, identity_mode=identity_mode)


__all__ = [
    "ANONYMOUS",
    "FORBIDDEN_MEMORY_TOKENS",
    "FORBIDDEN_PAYLOAD_KEYS",
    "GAME_ID_KEYS",
    "HUMAN_IDENTITY_KEYS",
    "PUBLIC_GAME_FIELDS",
    "PUBLIC_SEAT_FIELDS",
    "TRANSPARENT",
    "AnonymityViolation",
    "LeakFinding",
    "RankedPrivacyViolation",
    "assert_anonymous_safe",
    "assert_ranked_observation_safe",
    "audit_observation_log_for_seat",
    "coerce_identity_mode",
    "is_anonymous",
    "project_game_row",
    "project_row_through_allowlist",
    "project_seat_row",
]
