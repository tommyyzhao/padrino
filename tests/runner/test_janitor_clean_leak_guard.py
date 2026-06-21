"""US-177 Janitor clean leak-guard coverage.

The runner must use the NAR death-reveal output when it emits the public death
event. A cleaned death therefore carries no public role/faction reveal, even
when that death immediately ends the game; the true role remains recoverable
from internal role assignment/state and from the terminal reveal projection.
"""

from __future__ import annotations

from typing import Any

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.state import GameState
from padrino.core.enums import ActionType, Faction, IdentityMode, PhaseKind, Role
from padrino.core.observations import build_observation
from padrino.core.reveal import SeatRevealInput, project_endgame_reveal
from padrino.core.rulesets import mini7_v1
from padrino.core.spectator_projection import project_events_for_spectator_mode
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GameResume, drive_game_loop

_PHASE_ID = "NIGHT_1_ACTIONS"


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _append(state: GameState, log: EventLog, body: dict[str, Any]) -> GameState:
    sealed = {**body, "sequence": len(log.events)}
    log.append(sealed)
    return apply_event(state, EventAdapter.validate_python(sealed))


def _resumed_final_clean_state() -> tuple[GameState, EventLog]:
    state = initial_state()
    log = EventLog()
    state = _append(
        state,
        log,
        {
            "event_type": "GameCreated",
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": mini7_v1.RULESET_ID,
                "game_id": "G-JANITOR-LEAK",
                "game_seed": "janitor-leak-seed",
                "player_count": 5,
            },
        },
    )
    state = _append(
        state,
        log,
        {
            "event_type": "RolesAssigned",
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "assignments": [
                    {
                        "public_player_id": "P01",
                        "seat_index": 0,
                        "role": Role.MAFIA_GOON.value,
                        "faction": Faction.MAFIA.value,
                    },
                    {
                        "public_player_id": "P02",
                        "seat_index": 1,
                        "role": Role.JANITOR.value,
                        "faction": Faction.MAFIA.value,
                    },
                    {
                        "public_player_id": "P03",
                        "seat_index": 2,
                        "role": Role.VILLAGER.value,
                        "faction": Faction.TOWN.value,
                    },
                    {
                        "public_player_id": "P04",
                        "seat_index": 3,
                        "role": Role.VILLAGER.value,
                        "faction": Faction.TOWN.value,
                    },
                    {
                        "public_player_id": "P05",
                        "seat_index": 4,
                        "role": Role.VILLAGER.value,
                        "faction": Faction.TOWN.value,
                    },
                ]
            },
        },
    )
    return (
        _append(
            state,
            log,
            {
                "event_type": "PhaseStarted",
                "phase": _PHASE_ID,
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {
                    "phase_kind": PhaseKind.NIGHT_ACTIONS.value,
                    "day": 1,
                    "round": 0,
                },
            },
        ),
        log,
    )


def _payload_has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_payload_has_key(v, key) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_payload_has_key(item, key) for item in value)
    return False


async def test_cleaned_final_night_kill_has_no_midgame_role_leak_before_reveal() -> None:
    state, log = _resumed_final_clean_state()
    script = {
        (_PHASE_ID, "P01"): _response(ActionType.MAFIA_KILL, "P05"),
        (_PHASE_ID, "P02"): _response(ActionType.CLEAN, "P05"),
        (_PHASE_ID, "P03"): _response(ActionType.NOOP),
        (_PHASE_ID, "P04"): _response(ActionType.NOOP),
        (_PHASE_ID, "P05"): _response(ActionType.NOOP),
    }

    outcome = await drive_game_loop(
        GameConfig(
            game_id="G-JANITOR-LEAK",
            game_seed="janitor-leak-seed",
            ruleset_id=mini7_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(script),
        ranked=False,
        resume=GameResume(state=state, event_log=log, phase=_PHASE_ID),
    )

    bodies = [stored.body for stored in outcome.event_log.events]
    death = next(body for body in bodies if body["event_type"] == "PlayerEliminated")
    assert death["payload"] == {"public_player_id": "P05", "cause": "night_kill"}
    assert outcome.final_state.terminal_result == Faction.MAFIA.value

    for mode in (IdentityMode.ANONYMOUS, IdentityMode.TRANSPARENT):
        projected = project_events_for_spectator_mode(bodies, identity_mode=mode)
        projected_death = next(
            body for body in projected if body["event_type"] == "PlayerEliminated"
        )
        assert projected_death["payload"] == {"public_player_id": "P05", "cause": "night_kill"}
        for projected_event in projected:
            assert not _payload_has_key(projected_event.get("payload"), "role")
            assert not _payload_has_key(projected_event.get("payload"), "faction")

    for viewer in outcome.final_state.seats:
        obs = build_observation(outcome.final_state, viewer, outcome.event_log, mini7_v1)
        for public_event in obs.public_events:
            assert not _payload_has_key(public_event.payload, "role")
            assert not _payload_has_key(public_event.payload, "faction")

    reveal = project_endgame_reveal(
        game_id=outcome.final_state.game_id,
        ruleset_id=outcome.final_state.ruleset_id,
        winner=outcome.final_state.terminal_result,
        seats=(
            SeatRevealInput(
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                seat_kind=seat.seat_kind,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=seat.alive,
            )
            for seat in outcome.final_state.seats
        ),
    )
    revealed_target = next(seat for seat in reveal.seats if seat.public_player_id == "P05")
    assert revealed_target.role == Role.VILLAGER.value
    assert revealed_target.faction == Faction.TOWN.value
