"""Tests for :class:`padrino.llm.mock.DeterministicMockAdapter`."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult
from padrino.llm.mock import DeterministicMockAdapter


def _seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=True,
    )


SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _state(phase: Phase) -> GameState:
    return GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-MOCK",
        game_seed="seed",
        current_phase=phase,
        seats=SEATS,
        day=phase.day,
    )


def _observation(seat: Seat, phase: Phase) -> Observation:
    return build_observation(_state(phase), seat, EventLog(), mini7_v1)


def _resp(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def test_returns_scripted_response_for_known_key() -> None:
    canned = _resp(ActionType.VOTE, "P02")
    adapter = DeterministicMockAdapter({("DAY_1_VOTE", "P01"): canned})
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    result = asyncio.run(adapter.complete(obs))

    assert isinstance(result, AdapterResult)
    assert result.parsed_response == canned
    assert result.raw_response == canned.model_dump_json()
    assert result.latency_ms == 0
    assert result.status == "ok"


def test_records_each_invocation_in_calls_list_in_order() -> None:
    script = {
        ("DAY_1_VOTE", "P01"): _resp(ActionType.VOTE, "P02"),
        ("DAY_1_VOTE", "P03"): _resp(ActionType.ABSTAIN, None),
    }
    adapter = DeterministicMockAdapter(script)
    vote_phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)

    asyncio.run(adapter.complete(_observation(SEATS[0], vote_phase)))
    asyncio.run(adapter.complete(_observation(SEATS[2], vote_phase)))
    asyncio.run(adapter.complete(_observation(SEATS[0], vote_phase)))

    assert adapter.calls == [
        ("DAY_1_VOTE", "P01"),
        ("DAY_1_VOTE", "P03"),
        ("DAY_1_VOTE", "P01"),
    ]


def test_determinism_repeated_calls_return_identical_results() -> None:
    canned = _resp(ActionType.MAFIA_KILL, "P05")
    adapter = DeterministicMockAdapter({("NIGHT_1_ACTIONS", "P01"): canned})
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))

    first = asyncio.run(adapter.complete(obs))
    second = asyncio.run(adapter.complete(obs))

    assert first == second
    assert first.parsed_response is canned
    assert second.parsed_response is canned


def test_missing_key_raises_key_error_with_phase_and_player() -> None:
    adapter = DeterministicMockAdapter({})
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))

    with pytest.raises(KeyError) as exc_info:
        asyncio.run(adapter.complete(obs))

    assert exc_info.value.args[0] == ("DAY_1_VOTE", "P01")
    assert adapter.calls == [("DAY_1_VOTE", "P01")]


def test_phase_id_uses_canonical_format_for_night_actions() -> None:
    canned = _resp(ActionType.PROTECT, "P03")
    adapter = DeterministicMockAdapter({("NIGHT_1_ACTIONS", "P04"): canned})
    obs = _observation(SEATS[3], Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))

    result = asyncio.run(adapter.complete(obs))

    assert result.parsed_response == canned
    assert adapter.calls == [("NIGHT_1_ACTIONS", "P04")]


def test_phase_id_uses_canonical_format_for_discussion_round() -> None:
    canned = _resp(ActionType.NOOP, None)
    adapter = DeterministicMockAdapter({("DAY_2_DISCUSSION_ROUND_3", "P05"): canned})
    obs = _observation(SEATS[4], Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=3))

    result = asyncio.run(adapter.complete(obs))

    assert result.parsed_response == canned


def test_script_is_copied_so_caller_mutation_does_not_leak() -> None:
    canned = _resp(ActionType.ABSTAIN, None)
    script: dict[tuple[str, str], AgentResponse] = {("DAY_1_VOTE", "P01"): canned}
    adapter = DeterministicMockAdapter(script)

    script.clear()
    obs = _observation(SEATS[0], Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    result = asyncio.run(adapter.complete(obs))

    assert result.parsed_response == canned


def test_mock_adapter_module_has_no_forbidden_imports() -> None:
    src = Path("src/padrino/llm/mock.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"random", "secrets", "time", "datetime", "litellm", "httpx"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden, f"forbidden from-import: {node.module}"


# --- conftest helper sanity checks ----------------------------------------


def test_villager_script_helper_emits_noop_and_abstain_baseline() -> None:
    from tests.conftest import make_villager_script

    script = make_villager_script(
        seat_ids=("P05", "P06"),
        phase_ids=("DAY_1_VOTE", "DAY_1_DISCUSSION_ROUND_1", "NIGHT_1_ACTIONS"),
    )
    assert script[("DAY_1_VOTE", "P05")].action.type is ActionType.ABSTAIN
    assert script[("DAY_1_DISCUSSION_ROUND_1", "P06")].action.type is ActionType.NOOP
    assert script[("NIGHT_1_ACTIONS", "P05")].action.type is ActionType.NOOP


def test_villager_script_helper_applies_vote_overrides() -> None:
    from tests.conftest import make_villager_script

    script = make_villager_script(
        seat_ids=("P05", "P06"),
        phase_ids=("DAY_1_VOTE",),
        votes={"DAY_1_VOTE": {"P05": "P01"}},
    )
    p05 = script[("DAY_1_VOTE", "P05")]
    p06 = script[("DAY_1_VOTE", "P06")]
    assert p05.action.type is ActionType.VOTE
    assert p05.action.target == "P01"
    assert p06.action.type is ActionType.ABSTAIN


def test_mafia_script_helper_emits_mafia_kill_on_listed_night() -> None:
    from tests.conftest import make_mafia_script

    script = make_mafia_script(
        mafia_ids=("P01", "P02"),
        phase_ids=("NIGHT_1_ACTIONS", "DAY_1_VOTE", "NIGHT_1_MAFIA_DISCUSSION"),
        night_kill_targets={"NIGHT_1_ACTIONS": "P05"},
    )
    assert script[("NIGHT_1_ACTIONS", "P01")].action.type is ActionType.MAFIA_KILL
    assert script[("NIGHT_1_ACTIONS", "P01")].action.target == "P05"
    assert script[("NIGHT_1_ACTIONS", "P02")].action.target == "P05"
    assert script[("DAY_1_VOTE", "P01")].action.type is ActionType.ABSTAIN
    assert script[("NIGHT_1_MAFIA_DISCUSSION", "P01")].action.type is ActionType.NOOP


def test_town_win_script_covers_all_phases_and_seats() -> None:
    from tests.conftest import make_town_win_script, mini7_phase_ids

    script = make_town_win_script(
        mafia_ids=("P01", "P02"),
        town_ids=("P03", "P04", "P05", "P06", "P07"),
        doctor_id="P04",
        detective_id="P03",
    )

    expected_phases = set(mini7_phase_ids())
    expected_seats = {"P01", "P02", "P03", "P04", "P05", "P06", "P07"}
    assert {p for p, _ in script} == expected_phases
    assert {s for _, s in script} == expected_seats

    for sid in ("P03", "P04", "P05", "P06", "P07"):
        assert script[("DAY_1_VOTE", sid)].action == Action(type=ActionType.VOTE, target="P01")
        assert script[("DAY_2_VOTE", sid)].action == Action(type=ActionType.VOTE, target="P02")

    assert script[("NIGHT_1_ACTIONS", "P01")].action.type is ActionType.MAFIA_KILL
    assert script[("NIGHT_1_ACTIONS", "P04")].action.type is ActionType.PROTECT
    # Doctor protects same seat the mafia targets → kill is canceled.
    assert (
        script[("NIGHT_1_ACTIONS", "P04")].action.target
        == script[("NIGHT_1_ACTIONS", "P01")].action.target
    )
    assert script[("NIGHT_1_ACTIONS", "P03")].action == Action(
        type=ActionType.INVESTIGATE, target="P02"
    )


def test_mafia_win_script_kills_a_town_seat_each_night_one_through_three() -> None:
    from tests.conftest import make_mafia_win_script

    script = make_mafia_win_script(
        mafia_ids=("P01", "P02"),
        town_ids=("P03", "P04", "P05", "P06", "P07"),
    )
    for night, target in (
        ("NIGHT_1_ACTIONS", "P03"),
        ("NIGHT_2_ACTIONS", "P04"),
        ("NIGHT_3_ACTIONS", "P05"),
    ):
        for mid in ("P01", "P02"):
            assert script[(night, mid)].action == Action(type=ActionType.MAFIA_KILL, target=target)
    # Day votes all abstain so no eliminations come from town pressure.
    for sid in ("P03", "P04", "P05", "P06", "P07"):
        assert script[("DAY_1_VOTE", sid)].action.type is ActionType.ABSTAIN


def test_town_win_script_rejects_invalid_role_ids() -> None:
    from tests.conftest import make_town_win_script

    with pytest.raises(ValueError, match="doctor_id and detective_id"):
        make_town_win_script(
            mafia_ids=("P01", "P02"),
            town_ids=("P03", "P04", "P05", "P06", "P07"),
            doctor_id="P99",
            detective_id="P03",
        )


def test_mafia_win_script_rejects_undersized_rosters() -> None:
    from tests.conftest import make_mafia_win_script

    with pytest.raises(ValueError, match="mini7_v1"):
        make_mafia_win_script(mafia_ids=("P01",), town_ids=("P03", "P04", "P05"))
