"""Hypothesis stateful invariant tests for the mini7_v1 engine.

A :class:`RuleBasedStateMachine` drives a fresh mini-7 game phase-by-phase,
drawing a randomized :class:`Action` for every eligible seat at each step.
After every rule the engine-wide invariants spelled out in US-028 are
asserted; ``stateful_step_count=80`` is enough to exercise the full FSM
through to termination across the ``max_examples=50`` runs.

Lives under ``tests/core/`` because it is a pure-engine property test:
the only impure imports it touches are the in-process runner helpers,
which themselves stay synchronous in the driver below.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.phases import next_phase
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState
from padrino.core.engine.win_conditions import REASON_MAX_DAYS_REACHED, check_win
from padrino.core.enums import ActionType, PhaseKind, Role
from padrino.core.observations import format_phase_id
from padrino.core.rulesets import mini7_v1
from padrino.runner.game_runner import (
    _eligible_seats,
    _resolve_day_vote_events,
    _resolve_night_events,
    _submission_events_for,
)

_VISIBLE_MESSAGE_TYPES = frozenset({"PublicMessageSubmitted", "PrivateMessageSubmitted"})


def _action_strategy(seat_ids: list[str]) -> st.SearchStrategy[Action]:
    """Generate a mix of legal and illegal actions over the current roster.

    All action types are drawn uniformly regardless of role / phase so that
    the runner's phase-gated submission filter and the resolvers' coercion
    paths both see adversarial inputs.
    """
    targeted_types = (
        ActionType.VOTE,
        ActionType.MAFIA_KILL,
        ActionType.PROTECT,
        ActionType.INVESTIGATE,
    )
    return st.one_of(
        st.builds(Action, type=st.just(ActionType.NOOP), target=st.none()),
        st.builds(Action, type=st.just(ActionType.ABSTAIN), target=st.none()),
        st.builds(
            Action,
            type=st.sampled_from(targeted_types),
            target=st.sampled_from(seat_ids),
        ),
        st.builds(
            Action,
            type=st.sampled_from(targeted_types),
            target=st.none(),
        ),
    )


class GameMachine(RuleBasedStateMachine):
    """Drives a mini-7 game phase-by-phase under Hypothesis control."""

    def __init__(self) -> None:
        super().__init__()
        self.event_log: EventLog = EventLog()
        self.state: GameState = initial_state()
        self._terminated: bool = False
        self._prev_alive: int = mini7_v1.PLAYER_COUNT
        self._init_role_counts: dict[Role, int] = {}
        self._bootstrap_game()

    def _emit(self, body: dict[str, Any]) -> None:
        sealed = dict(body)
        sealed["sequence"] = len(self.event_log.events)
        self.event_log.append(sealed)
        event = EventAdapter.validate_python(sealed)
        self.state = apply_event(self.state, event)

    def _bootstrap_game(self) -> None:
        self._emit(
            {
                "event_type": "GameCreated",
                "phase": "SETUP",
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {
                    "ruleset_id": mini7_v1.RULESET_ID,
                    "game_id": "hyp-game",
                    "game_seed": "hyp-seed",
                    "player_count": mini7_v1.PLAYER_COUNT,
                },
            }
        )
        seats = assign_roles("hyp-seed", mini7_v1)
        self._emit(
            {
                "event_type": "RolesAssigned",
                "phase": "SETUP",
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {
                    "assignments": [
                        {
                            "public_player_id": s.public_player_id,
                            "seat_index": s.seat_index,
                            "role": s.role.value,
                            "faction": s.faction.value,
                        }
                        for s in seats
                    ],
                },
            }
        )
        for s in self.state.seats:
            self._init_role_counts[s.role] = self._init_role_counts.get(s.role, 0) + 1

    @rule(data=st.data())
    def step_phase(self, data: st.DataObject) -> None:
        if self._terminated:
            return
        candidate = next_phase(self.state.current_phase, mini7_v1)
        if candidate.kind is PhaseKind.TERMINAL:
            self._emit(
                {
                    "event_type": "GameTerminated",
                    "phase": "TERMINAL",
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": {
                        "winner": "DRAW",
                        "reason": REASON_MAX_DAYS_REACHED,
                    },
                }
            )
            self._terminated = True
            return

        phase_id = format_phase_id(candidate)
        self._emit(
            {
                "event_type": "PhaseStarted",
                "phase": phase_id,
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {
                    "phase_kind": candidate.kind.value,
                    "day": candidate.day,
                    "round": candidate.round,
                },
            }
        )

        eligible = _eligible_seats(self.state)
        seat_ids = [s.public_player_id for s in self.state.seats]
        responses: dict[str, AgentResponse] = {}
        for seat in eligible:
            action = data.draw(_action_strategy(seat_ids))
            responses[seat.public_player_id] = AgentResponse(
                public_message=None,
                private_message=None,
                action=action,
                memory_update="",
                rationale_summary=None,
            )

        for seat in eligible:
            response = responses[seat.public_player_id]
            for body in _submission_events_for(
                seat,
                response,
                candidate.kind,
                phase_id,
                candidate.round,
            ):
                self._emit(body)

        if candidate.kind is PhaseKind.DAY_VOTE:
            for body in _resolve_day_vote_events(self.state, responses, phase_id):
                self._emit(body)
        elif candidate.kind is PhaseKind.NIGHT_ACTIONS:
            for body in _resolve_night_events(self.state, responses, phase_id):
                self._emit(body)

        self._emit(
            {
                "event_type": "PhaseResolved",
                "phase": phase_id,
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {"resolved_phase": phase_id},
            }
        )

        win = check_win(self.state, mini7_v1)
        if win is not None:
            self._emit(
                {
                    "event_type": "GameTerminated",
                    "phase": phase_id,
                    "visibility": "PUBLIC",
                    "actor_player_id": None,
                    "payload": {"winner": win.winner, "reason": win.reason},
                }
            )
            self._terminated = True

    @invariant()
    def alive_count_monotone_non_increasing(self) -> None:
        cur = sum(1 for s in self.state.seats if s.alive)
        assert cur <= self._prev_alive, f"alive_count rose from {self._prev_alive} to {cur}"
        self._prev_alive = cur

    @invariant()
    def player_dies_at_most_once(self) -> None:
        seen: set[str] = set()
        for ev in self.event_log.events:
            if ev.body["event_type"] != "PlayerEliminated":
                continue
            pid = ev.body["payload"]["public_player_id"]
            assert pid not in seen, f"player {pid} eliminated more than once"
            seen.add(pid)

    @invariant()
    def dead_players_never_speak(self) -> None:
        died_at: dict[str, int] = {}
        for ev in self.event_log.events:
            if ev.body["event_type"] == "PlayerEliminated":
                pid = ev.body["payload"]["public_player_id"]
                died_at.setdefault(pid, ev.sequence)
        for ev in self.event_log.events:
            if ev.body["event_type"] not in _VISIBLE_MESSAGE_TYPES:
                continue
            actor = ev.body.get("actor_player_id")
            if actor is None:
                continue
            death_seq = died_at.get(actor)
            if death_seq is not None and ev.sequence > death_seq:
                msg = f"dead player {actor} spoke at sequence {ev.sequence}"
                raise AssertionError(msg)

    @invariant()
    def role_counts_never_change(self) -> None:
        counts: dict[Role, int] = {}
        for s in self.state.seats:
            counts[s.role] = counts.get(s.role, 0) + 1
        # Pre-bootstrap the machine has no seats yet — skip until roles assigned.
        if not self._init_role_counts:
            return
        assert counts == self._init_role_counts, (
            f"role counts drifted: {counts} != {self._init_role_counts}"
        )

    @invariant()
    def at_most_one_terminal_result(self) -> None:
        terminals = sum(
            1 for ev in self.event_log.events if ev.body["event_type"] == "GameTerminated"
        )
        assert terminals <= 1, f"{terminals} GameTerminated events emitted"
        if terminals == 1:
            assert self.state.terminal_result is not None
            assert self.state.terminal_reason is not None
        else:
            assert self.state.terminal_result is None
            assert self.state.terminal_reason is None

    @invariant()
    def replay_reproduces_live_state(self) -> None:
        typed_events = [EventAdapter.validate_python(ev.body) for ev in self.event_log.events]
        replayed_state = replay_events(typed_events)
        assert replayed_state == self.state
        replayed_log = replay_event_log(self.event_log.events)
        original = self.event_log.events
        replayed = replayed_log.events
        assert len(original) == len(replayed)
        for a, b in zip(original, replayed, strict=True):
            assert a.sequence == b.sequence
            assert a.prev_event_hash == b.prev_event_hash
            assert a.event_hash == b.event_hash


GameMachine.TestCase.settings = hyp_settings(
    max_examples=50,
    stateful_step_count=80,
    deadline=None,
)

TestGameInvariants = GameMachine.TestCase


def test_invariants_module_has_no_forbidden_imports() -> None:
    """The invariant test file may not depend on wall-clock / RNG smuggling."""
    src = Path(__file__).read_text()
    tree = ast.parse(src)
    forbidden = {"random", "secrets", "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden, f"forbidden import: {node.module}"
