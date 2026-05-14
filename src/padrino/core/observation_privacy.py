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

from typing import Any, Final

from padrino.core.observations import Observation


class RankedPrivacyViolation(ValueError):
    """Raised when a ranked observation contains forbidden information."""


FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
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


__all__ = [
    "FORBIDDEN_MEMORY_TOKENS",
    "FORBIDDEN_PAYLOAD_KEYS",
    "GAME_ID_KEYS",
    "RankedPrivacyViolation",
    "assert_ranked_observation_safe",
]
