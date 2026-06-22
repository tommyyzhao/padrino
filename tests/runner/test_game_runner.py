"""Tests for :func:`padrino.runner.game_runner.run_game`.

Drives a full mini7_v1 game through the deterministic mock adapter and asserts:
the terminal state matches the scripted outcome, the event log's hash chain
replays cleanly, the reducer fold over the event stream reproduces the final
state, no events follow ``GameTerminated``, eligibility never includes dead
seats, and ``llm_calls`` captures every adapter dispatch.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.events import Event, EventAdapter
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.win_conditions import REASON_MAX_DAYS_REACHED
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.rulesets import (
    Ruleset,
    bench10_v1,
    mini7_v1,
    ninja13_v1,
    roleblock10_v1,
    visit12_v1,
)
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import (
    make_mafia_win_script,
    make_town_win_script,
    make_villager_script,
    mini7_phase_ids,
)

_GAME_SEED = "seed-runner-001"


def _split_factions() -> tuple[list[str], list[str], str, str]:
    """Return (mafia_ids, town_ids, doctor_id, detective_id) for the seed."""
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


def _adapter(script: Mapping[tuple[str, str], AgentResponse]) -> DeterministicMockAdapter:
    return DeterministicMockAdapter(script)


def _config() -> GameConfig:
    return GameConfig(game_id="G-RUNNER", game_seed=_GAME_SEED, timeout_s=1.0)


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _phase_ids_for(ruleset: Ruleset) -> tuple[str, ...]:
    """All promptable phase ids for the current canonical phase skeleton."""
    out: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for day in range(1, ruleset.MAX_DAYS + 1):
        for round_index in range(1, ruleset.DISCUSSION_ROUNDS_PER_DAY + 1):
            out.append(f"DAY_{day}_DISCUSSION_ROUND_{round_index}")
        out.append(f"DAY_{day}_VOTE")
        out.append(f"NIGHT_{day}_MAFIA_DISCUSSION")
        out.append(f"NIGHT_{day}_ACTIONS")
    return tuple(out)


async def _passive_draw_outcome(ruleset: Ruleset, *, seed: str, game_id: str) -> GameOutcome:
    seats = assign_roles(seed, ruleset)
    seat_ids = [seat.public_player_id for seat in seats]
    script = make_villager_script(seat_ids, _phase_ids_for(ruleset))
    config = GameConfig(
        game_id=game_id,
        game_seed=seed,
        ruleset_id=ruleset.RULESET_ID,
        timeout_s=1.0,
    )
    return await run_game(config, _adapter(script), ranked=False)


def _typed_events(outcome: GameOutcome) -> list[Event]:
    return [EventAdapter.validate_python(stored.body) for stored in outcome.event_log.events]


# --- terminal scenarios -----------------------------------------------------


async def test_town_win_scenario_terminates_with_town_winner() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    assert outcome.final_state.terminal_result == "TOWN"
    bodies = [stored.body for stored in outcome.event_log.events]
    final = bodies[-1]
    assert final["event_type"] == "GameTerminated"
    assert final["payload"]["winner"] == "TOWN"


async def test_mafia_win_scenario_terminates_with_mafia_winner() -> None:
    mafia, town, _, _ = _split_factions()
    script = make_mafia_win_script(mafia_ids=mafia, town_ids=town)
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    assert outcome.final_state.terminal_result == "MAFIA"
    bodies = [stored.body for stored in outcome.event_log.events]
    assert bodies[-1]["payload"]["winner"] == "MAFIA"


async def test_draw_scenario_terminates_at_max_days() -> None:
    mafia, town, _, _ = _split_factions()
    seat_ids = mafia + town
    script = make_villager_script(seat_ids, mini7_phase_ids())
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    assert outcome.final_state.terminal_result == "DRAW"
    bodies = [stored.body for stored in outcome.event_log.events]
    assert bodies[-1]["event_type"] == "GameTerminated"
    assert bodies[-1]["payload"]["winner"] == "DRAW"
    assert bodies[-1]["payload"]["reason"] == REASON_MAX_DAYS_REACHED


async def test_passive_day_cap_hash_chain_stable_for_canonical_rulesets() -> None:
    for ruleset in (mini7_v1, bench10_v1, roleblock10_v1, visit12_v1, ninja13_v1):
        ruleset_id = ruleset.RULESET_ID
        first = await _passive_draw_outcome(
            ruleset, seed=f"{ruleset_id}-day-cap", game_id=f"G-{ruleset_id}"
        )
        second = await _passive_draw_outcome(
            ruleset, seed=f"{ruleset_id}-day-cap", game_id=f"G-{ruleset_id}"
        )

        first_bodies = [stored.body for stored in first.event_log.events]
        second_bodies = [stored.body for stored in second.event_log.events]
        assert first_bodies[-1]["payload"] == {
            "winner": "DRAW",
            "reason": REASON_MAX_DAYS_REACHED,
        }
        assert first.final_state.terminal_result == "DRAW"
        assert first.final_state.terminal_reason == REASON_MAX_DAYS_REACHED
        assert first_bodies[-1] == second_bodies[-1]
        assert [event.event_hash for event in first.event_log.events] == [
            event.event_hash for event in second.event_log.events
        ]


async def test_roleblock10_runner_blocks_detective_with_structured_feedback() -> None:
    seed = "roleblock-runner-001"
    seats = assign_roles(seed, roleblock10_v1)
    seat_ids = [seat.public_player_id for seat in seats]
    goons = [s.public_player_id for s in seats if s.role is Role.MAFIA_GOON]
    roleblocker = next(s.public_player_id for s in seats if s.role is Role.MAFIA_ROLEBLOCKER)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    target = next(
        s.public_player_id for s in seats if s.faction is Faction.TOWN and s.role is Role.VILLAGER
    )
    phase_ids = _phase_ids_for(roleblock10_v1)
    script = make_villager_script(seat_ids, phase_ids)
    for goon in goons:
        script[("NIGHT_1_ACTIONS", goon)] = _response(ActionType.MAFIA_KILL, target)
    script[("NIGHT_1_ACTIONS", roleblocker)] = _response(ActionType.ROLEBLOCK, detective)
    script[("NIGHT_1_ACTIONS", detective)] = _response(ActionType.INVESTIGATE, goons[0])

    outcome = await run_game(
        GameConfig(
            game_id="G-ROLEBLOCK-RUNNER",
            game_seed=seed,
            ruleset_id=roleblock10_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        _adapter(script),
        ranked=False,
    )
    bodies = [stored.body for stored in outcome.event_log.events]

    assert any(
        body["event_type"] == "RoleblockSubmitted"
        and body["actor_player_id"] == roleblocker
        and body["payload"] == {"target": detective}
        for body in bodies
    )
    assert any(
        body["event_type"] == "PlayerEliminated"
        and body["phase"] == "NIGHT_1_ACTIONS"
        and body["payload"]["public_player_id"] == target
        for body in bodies
    )
    assert not any(
        body["event_type"] == "DetectiveResultDelivered"
        and body["phase"] == "NIGHT_1_ACTIONS"
        and body["actor_player_id"] == detective
        for body in bodies
    )
    feedback = [
        body
        for body in bodies
        if body["event_type"] == "NightFeedbackDelivered" and body["actor_player_id"] == detective
    ]
    assert len(feedback) == 1
    assert feedback[0]["phase"] == "NIGHT_1_ACTIONS"
    assert feedback[0]["visibility"] == "PRIVATE"
    assert feedback[0]["payload"] == {
        "code": "ACTION_BLOCKED",
        "target": goons[0],
        "finding": None,
        "visited_player_ids": (),
        "visitor_player_ids": (),
    }


async def test_visit12_runner_delivers_track_watch_structured_feedback() -> None:
    seed = "visit-runner-001"
    seats = assign_roles(seed, visit12_v1)
    seat_ids = [seat.public_player_id for seat in seats]
    goons = [s.public_player_id for s in seats if s.role is Role.MAFIA_GOON]
    roleblocker = next(s.public_player_id for s in seats if s.role is Role.MAFIA_ROLEBLOCKER)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    tracker = next(s.public_player_id for s in seats if s.role is Role.TRACKER)
    watcher = next(s.public_player_id for s in seats if s.role is Role.WATCHER)
    kill_target = next(
        s.public_player_id for s in seats if s.faction is Faction.TOWN and s.role is Role.VILLAGER
    )
    phase_ids = _phase_ids_for(visit12_v1)
    script = make_villager_script(seat_ids, phase_ids)
    for goon in goons:
        script[("NIGHT_1_ACTIONS", goon)] = _response(ActionType.MAFIA_KILL, kill_target)
    script[("NIGHT_1_ACTIONS", roleblocker)] = _response(ActionType.ROLEBLOCK, detective)
    script[("NIGHT_1_ACTIONS", detective)] = _response(ActionType.INVESTIGATE, goons[0])
    script[("NIGHT_1_ACTIONS", tracker)] = _response(ActionType.TRACK, roleblocker)
    script[("NIGHT_1_ACTIONS", watcher)] = _response(ActionType.WATCH, detective)

    outcome = await run_game(
        GameConfig(
            game_id="G-VISIT-RUNNER",
            game_seed=seed,
            ruleset_id=visit12_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        _adapter(script),
        ranked=False,
    )
    bodies = [stored.body for stored in outcome.event_log.events]

    assert any(
        body["event_type"] == "TrackSubmitted"
        and body["actor_player_id"] == tracker
        and body["payload"] == {"target": roleblocker}
        for body in bodies
    )
    assert any(
        body["event_type"] == "WatchSubmitted"
        and body["actor_player_id"] == watcher
        and body["payload"] == {"target": detective}
        for body in bodies
    )
    feedback = {
        body["actor_player_id"]: body["payload"]
        for body in bodies
        if body["event_type"] == "NightFeedbackDelivered"
        and body["actor_player_id"] in {tracker, watcher}
    }
    assert feedback == {
        tracker: {
            "code": "TRACK_RESULT",
            "target": roleblocker,
            "finding": None,
            "visited_player_ids": (detective,),
            "visitor_player_ids": (),
        },
        watcher: {
            "code": "WATCH_RESULT",
            "target": detective,
            "finding": None,
            "visited_player_ids": (),
            "visitor_player_ids": (roleblocker,),
        },
    }


async def test_ninja13_runner_suppresses_ninja_kill_visit_only() -> None:
    seed = "ninja-runner-001"
    seats = assign_roles(seed, ninja13_v1)
    seat_ids = [seat.public_player_id for seat in seats]
    ninja = next(s.public_player_id for s in seats if s.role is Role.NINJA)
    roleblocker = next(s.public_player_id for s in seats if s.role is Role.MAFIA_ROLEBLOCKER)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    tracker = next(s.public_player_id for s in seats if s.role is Role.TRACKER)
    watcher = next(s.public_player_id for s in seats if s.role is Role.WATCHER)
    kill_target = next(
        s.public_player_id for s in seats if s.faction is Faction.TOWN and s.role is Role.VILLAGER
    )
    phase_ids = _phase_ids_for(ninja13_v1)
    script = make_villager_script(seat_ids, phase_ids)
    script[("NIGHT_1_ACTIONS", ninja)] = _response(ActionType.MAFIA_KILL, kill_target)
    script[("NIGHT_1_ACTIONS", roleblocker)] = _response(ActionType.ROLEBLOCK, detective)
    script[("NIGHT_1_ACTIONS", detective)] = _response(ActionType.INVESTIGATE, ninja)
    script[("NIGHT_1_ACTIONS", tracker)] = _response(ActionType.TRACK, ninja)
    script[("NIGHT_1_ACTIONS", watcher)] = _response(ActionType.WATCH, detective)

    outcome = await run_game(
        GameConfig(
            game_id="G-NINJA-RUNNER",
            game_seed=seed,
            ruleset_id=ninja13_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        _adapter(script),
        ranked=False,
    )
    bodies = [stored.body for stored in outcome.event_log.events]

    assert any(
        body["event_type"] == "PlayerEliminated"
        and body["phase"] == "NIGHT_1_ACTIONS"
        and body["payload"]["public_player_id"] == kill_target
        for body in bodies
    )
    feedback = {
        body["actor_player_id"]: body["payload"]
        for body in bodies
        if body["event_type"] == "NightFeedbackDelivered"
        and body["actor_player_id"] in {tracker, watcher}
    }
    assert feedback == {
        tracker: {
            "code": "TRACK_RESULT",
            "target": ninja,
            "finding": None,
            "visited_player_ids": (),
            "visitor_player_ids": (),
        },
        watcher: {
            "code": "WATCH_RESULT",
            "target": detective,
            "finding": None,
            "visited_player_ids": (),
            "visitor_player_ids": (roleblocker,),
        },
    }


# --- log shape & invariants -------------------------------------------------


async def test_event_log_starts_with_game_created_and_roles_assigned() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    bodies = [stored.body for stored in outcome.event_log.events]
    assert bodies[0]["event_type"] == "GameCreated"
    assert bodies[0]["payload"]["game_id"] == "G-RUNNER"
    assert bodies[0]["payload"]["game_seed"] == _GAME_SEED
    assert bodies[1]["event_type"] == "RolesAssigned"
    assert len(bodies[1]["payload"]["assignments"]) == mini7_v1.PLAYER_COUNT


async def test_hash_chain_replays_cleanly() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    # replay_event_log raises on any tampered or non-matching hash.
    replayed = replay_event_log(outcome.event_log.events)
    assert len(replayed.events) == len(outcome.event_log.events)
    for original, repeated in zip(outcome.event_log.events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.prev_event_hash == repeated.prev_event_hash
        assert original.sequence == repeated.sequence


async def test_reducer_fold_reproduces_final_state() -> None:
    mafia, town, _, _ = _split_factions()
    script = make_mafia_win_script(mafia_ids=mafia, town_ids=town)
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    typed = _typed_events(outcome)
    replayed_state = replay_events(typed)
    assert replayed_state.terminal_result == outcome.final_state.terminal_result
    assert replayed_state.terminal_reason == outcome.final_state.terminal_reason
    assert replayed_state.seats == outcome.final_state.seats
    assert replayed_state.current_phase == outcome.final_state.current_phase


async def test_no_events_after_game_terminated() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    events = outcome.event_log.events
    terminated_idx = next(
        i for i, stored in enumerate(events) if stored.body["event_type"] == "GameTerminated"
    )
    assert terminated_idx == len(events) - 1


async def test_sequence_numbers_are_contiguous() -> None:
    mafia, town, _, _ = _split_factions()
    script = make_mafia_win_script(mafia_ids=mafia, town_ids=town)
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    sequences = [stored.sequence for stored in outcome.event_log.events]
    assert sequences == list(range(len(sequences)))


async def test_dead_seats_never_dispatched_after_elimination() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    adapter = _adapter(script)
    outcome = await run_game(_config(), adapter, ranked=False)

    # After D1 vote eliminates mafia[0], mafia[0] should never be called again.
    eliminations: dict[str, int] = {}
    for stored in outcome.event_log.events:
        body = stored.body
        if body["event_type"] == "PlayerEliminated":
            eliminations[body["payload"]["public_player_id"]] = stored.sequence

    assert eliminations, "town-win scenario must eliminate at least one seat"

    # The eliminated mafia seat must be dispatched strictly fewer times than a
    # seat that survives the entire game; the runner must stop ticking dead seats.
    total_phases_per_seat: dict[str, int] = {}
    for _, seat_id in adapter.calls:
        total_phases_per_seat[seat_id] = total_phases_per_seat.get(seat_id, 0) + 1
    eliminated_seat = mafia[0]
    survivor_seat = doctor
    assert total_phases_per_seat[eliminated_seat] < total_phases_per_seat[survivor_seat]


async def test_llm_calls_collected_for_every_dispatch() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    adapter = _adapter(script)
    outcome = await run_game(_config(), adapter, ranked=False)
    assert len(outcome.llm_calls) == len(adapter.calls)
    assert all(call.latency_ms == 0 for call in outcome.llm_calls)


async def test_action_submission_events_match_phase_kind() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    bodies = [stored.body for stored in outcome.event_log.events]

    # VoteSubmitted only appears under DAY_*_VOTE phases.
    for body in bodies:
        if body["event_type"] == "VoteSubmitted":
            assert body["phase"].endswith("_VOTE")
        if body["event_type"] in (
            "MafiaKillVoteSubmitted",
            "ProtectSubmitted",
            "InvestigateSubmitted",
        ):
            assert body["phase"].endswith("_ACTIONS")


async def test_protect_submission_updates_doctor_last_protected_target() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    doctor_seat = next(s for s in outcome.final_state.seats if s.public_player_id == doctor)
    # The town-win script protects a single specific town seat on N1.
    expected_protect_target = next(t for t in town if t != doctor)
    assert doctor_seat.last_protected_target == expected_protect_target


async def test_detective_finding_emitted_on_investigation() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    detective_events = [
        stored.body
        for stored in outcome.event_log.events
        if stored.body["event_type"] == "DetectiveResultDelivered"
    ]
    assert len(detective_events) == 1
    payload = detective_events[0]["payload"]
    assert payload["target"] == mafia[1]
    assert payload["finding"] == "MAFIA"


# --- coercion path ----------------------------------------------------------


class _SilentAdapter:
    """Adapter that always returns a NOOP+ABSTAIN response (legal in every phase)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, observation):  # type: ignore[no-untyped-def]
        from padrino.llm.adapter import AdapterResult

        self.calls += 1
        is_vote = observation.phase.endswith("_VOTE")
        action = Action(
            type=ActionType.ABSTAIN if is_vote else ActionType.NOOP,
            target=None,
        )
        response = AgentResponse(
            public_message=None,
            private_message=None,
            action=action,
            memory_update="",
            rationale_summary=None,
        )
        return AdapterResult(
            raw_response="{}",
            parsed_response=response,
            latency_ms=0,
        )


async def test_passive_adapter_with_no_resolutions_drives_draw() -> None:
    """Smoke test: a fully passive adapter reaches a DRAW via the FSM."""
    adapter = _SilentAdapter()
    outcome = await run_game(_config(), adapter, ranked=False)
    assert outcome.final_state.terminal_result == "DRAW"
    assert outcome.final_state.terminal_reason == REASON_MAX_DAYS_REACHED


# --- purity guard -----------------------------------------------------------


def test_game_runner_does_not_import_forbidden_modules() -> None:
    """The runner is allowed impure imports but must not touch wall-clock or random."""
    src = Path("src/padrino/runner/game_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"random", "secrets", "datetime", "time"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), f"forbidden imports: {imported & forbidden}"
