"""Deterministic replay primitives.

Two pure entry points, both depending only on already-shipped pure-core modules:

- :func:`replay_events` folds a typed :class:`Event` sequence through the
  reducer to reconstruct the final :class:`GameState`. This is the "engine
  replay" mode — given the events, recover the state.

- :func:`replay_event_log` re-appends the bodies of prior
  :class:`StoredEvent` records to a fresh :class:`EventLog`, verifying that
  the regenerated chain reproduces every original sequence number and
  ``event_hash``. ``created_at`` is excluded from the hash by construction,
  so replay with a different clock value still verifies.

The LLM frozen-response replay (consume archived ``AgentResponse`` objects
through the engine loop to regenerate the event log) is intentionally not in
this module: it depends on US-021 (AgentResponse) and US-029 (GameRunner /
GameConfig), which have not yet shipped.
"""

from __future__ import annotations

from collections.abc import Sequence

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.events import Event, GameCreated
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.state import GameState
from padrino.core.rulesets.snapshot import GameRulesSnapshot, assert_ruleset_id_matches_snapshot


class ReplayHashMismatchError(ValueError):
    """Raised when a replayed event's hash disagrees with the stored hash.

    Attribute ``sequence`` carries the sequence number of the first event
    whose recomputed ``event_hash`` did not match the stored value.
    """

    def __init__(self, sequence: int, expected: str, actual: str) -> None:
        super().__init__(
            f"replay hash mismatch at sequence {sequence}: expected {expected}, got {actual}"
        )
        self.sequence = sequence
        self.expected = expected
        self.actual = actual


class ReplayRulesSnapshotMismatchError(ValueError):
    """Raised when archived events disagree with the supplied rules snapshot."""


def replay_events(
    events: Sequence[Event],
    *,
    rules_snapshot: GameRulesSnapshot | None = None,
) -> GameState:
    """Fold ``events`` through :func:`apply_event` and return the final state."""
    if events:
        _validate_game_created_event(events[0], rules_snapshot)
    state = initial_state()
    for event in events:
        state = apply_event(state, event)
    return state


def replay_event_log(
    stored_events: Sequence[StoredEvent],
    *,
    rules_snapshot: GameRulesSnapshot | None = None,
) -> EventLog:
    """Re-seal each prior body through a fresh :class:`EventLog` and verify."""
    log = EventLog()
    for prior in stored_events:
        if prior.sequence == 0:
            _validate_game_created_body(prior.body, rules_snapshot)
        replayed = log.append(prior.body)
        if replayed.event_hash != prior.event_hash:
            raise ReplayHashMismatchError(
                sequence=prior.sequence,
                expected=prior.event_hash,
                actual=replayed.event_hash,
            )
    return log


def _validate_game_created_event(
    event: Event,
    rules_snapshot: GameRulesSnapshot | None,
) -> None:
    if rules_snapshot is None or not isinstance(event, GameCreated):
        return
    _validate_game_created_payload(
        ruleset_id=event.payload.ruleset_id,
        player_count=event.payload.player_count,
        rules_snapshot=rules_snapshot,
    )


def _validate_game_created_body(
    body: dict[str, object],
    rules_snapshot: GameRulesSnapshot | None,
) -> None:
    if rules_snapshot is None or body.get("event_type") != "GameCreated":
        return
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise ReplayRulesSnapshotMismatchError("GameCreated payload is missing")
    ruleset_id = payload.get("ruleset_id")
    player_count = payload.get("player_count")
    if not isinstance(ruleset_id, str) or not isinstance(player_count, int):
        raise ReplayRulesSnapshotMismatchError("GameCreated payload is malformed")
    _validate_game_created_payload(
        ruleset_id=ruleset_id,
        player_count=player_count,
        rules_snapshot=rules_snapshot,
    )


def _validate_game_created_payload(
    *,
    ruleset_id: str,
    player_count: int,
    rules_snapshot: GameRulesSnapshot,
) -> None:
    try:
        assert_ruleset_id_matches_snapshot(ruleset_id=ruleset_id, snapshot=rules_snapshot)
    except ValueError as exc:
        raise ReplayRulesSnapshotMismatchError(str(exc)) from exc
    if player_count != rules_snapshot.PLAYER_COUNT:
        raise ReplayRulesSnapshotMismatchError(
            "rules_snapshot_player_count_mismatch:"
            f" expected {rules_snapshot.PLAYER_COUNT}, got {player_count}"
        )


__all__ = [
    "ReplayHashMismatchError",
    "ReplayRulesSnapshotMismatchError",
    "replay_event_log",
    "replay_events",
]
