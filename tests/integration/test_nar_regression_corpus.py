"""Regression corpus pinning real-action NAR event hash chains."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import Seat
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.rulesets import (
    BUILTIN_RULESET_IDS,
    Ruleset,
    bench10_v1,
    deception13_v1,
    get_ruleset,
    jester8_v1,
    mini7_v1,
    ninja13_v1,
    roleblock10_v1,
    sk12_v1,
    visit12_v1,
)
from padrino.llm.mock import DeterministicMockAdapter, NoopMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import (
    ScriptedAction,
    make_role_aware_script,
    make_town_win_script,
    phase_ids_for_ruleset,
)

Script = Mapping[tuple[str, str], AgentResponse]


@dataclass(frozen=True, slots=True)
class _CorpusCase:
    ruleset_id: str
    case_id: str
    game_id: str
    seed: str
    adapter_factory: Callable[[], DeterministicMockAdapter | NoopMockAdapter]
    terminal_result: str
    terminal_reason: str
    event_count: int
    final_event_hash: str
    event_hash_chain_sha256: str
    assertion: Callable[[GameOutcome], None]
    real_action: bool


def _event_hash_chain_digest(outcome: GameOutcome) -> str:
    joined = "\n".join(stored.event_hash for stored in outcome.event_log.events)
    return hashlib.sha256(joined.encode()).hexdigest()


def _bodies(outcome: GameOutcome, event_type: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        stored.body
        for stored in outcome.event_log.events
        if stored.body["event_type"] == event_type
    )


def _payloads(outcome: GameOutcome, event_type: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(cast(Mapping[str, Any], body["payload"]) for body in _bodies(outcome, event_type))


def _seat_ids(seats: Sequence[Seat]) -> tuple[str, ...]:
    return tuple(seat.public_player_id for seat in seats)


def _role_ids(seats: Sequence[Seat], role: Role) -> tuple[str, ...]:
    return tuple(seat.public_player_id for seat in seats if seat.role is role)


def _role_id(seats: Sequence[Seat], role: Role) -> str:
    ids = _role_ids(seats, role)
    if not ids:
        raise AssertionError(f"missing role in fixture: {role.value}")
    return ids[0]


def _first_villager(seats: Sequence[Seat], *excluded: str) -> str:
    blocked = set(excluded)
    for seat in seats:
        if seat.role is Role.VILLAGER and seat.public_player_id not in blocked:
            return seat.public_player_id
    raise AssertionError("fixture has no villager target")


def _vote_out(
    actions: dict[tuple[str, str], ScriptedAction],
    *,
    seat_ids: tuple[str, ...],
    phase_id: str,
    target: str,
) -> None:
    fallback = next(seat_id for seat_id in seat_ids if seat_id != target)
    for seat_id in seat_ids:
        actions[(phase_id, seat_id)] = ScriptedAction(
            ActionType.VOTE,
            fallback if seat_id == target else target,
        )


def _script_from_actions(
    ruleset: Ruleset,
    seats: Sequence[Seat],
    actions: Mapping[tuple[str, str], ScriptedAction],
) -> Script:
    return make_role_aware_script(
        _seat_ids(seats),
        phase_ids_for_ruleset(ruleset),
        actions=actions,
    )


def _mini7_town_win_adapter() -> DeterministicMockAdapter:
    seed = "nar-corpus-mini7-town"
    seats = assign_roles(seed, mini7_v1)
    mafia_ids = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town_ids = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor_id = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective_id = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return DeterministicMockAdapter(
        make_town_win_script(
            mafia_ids=mafia_ids,
            town_ids=town_ids,
            doctor_id=doctor_id,
            detective_id=detective_id,
        )
    )


def _bench10_draw_adapter() -> NoopMockAdapter:
    return NoopMockAdapter()


def _roleblock10_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-roleblock10-real", roleblock10_v1)
    seat_ids = _seat_ids(seats)
    mafia_goons = _role_ids(seats, Role.MAFIA_GOON)
    roleblocker = _role_id(seats, Role.MAFIA_ROLEBLOCKER)
    detective = _role_id(seats, Role.DETECTIVE)
    night_kill_target = _first_villager(seats)
    actions: dict[tuple[str, str], ScriptedAction] = {}

    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=mafia_goons[0])
    actions[("NIGHT_1_ACTIONS", roleblocker)] = ScriptedAction(ActionType.ROLEBLOCK, detective)
    actions[("NIGHT_1_ACTIONS", mafia_goons[1])] = ScriptedAction(
        ActionType.MAFIA_KILL,
        night_kill_target,
    )
    actions[("NIGHT_1_ACTIONS", detective)] = ScriptedAction(
        ActionType.INVESTIGATE,
        roleblocker,
    )
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_2_VOTE", target=roleblocker)
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_3_VOTE", target=mafia_goons[1])

    return DeterministicMockAdapter(_script_from_actions(roleblock10_v1, seats, actions))


def _deception13_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-deception13-real", deception13_v1)
    seat_ids = _seat_ids(seats)
    godfather = _role_id(seats, Role.GODFATHER)
    roleblocker = _role_id(seats, Role.MAFIA_ROLEBLOCKER)
    janitor = _role_id(seats, Role.JANITOR)
    mafia_goon = _role_id(seats, Role.MAFIA_GOON)
    detective = _role_id(seats, Role.DETECTIVE)
    doctor = _role_id(seats, Role.DOCTOR)
    victim = _first_villager(seats)
    actions: dict[tuple[str, str], ScriptedAction] = {}

    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=mafia_goon)
    actions[("NIGHT_1_ACTIONS", godfather)] = ScriptedAction(ActionType.MAFIA_KILL, victim)
    actions[("NIGHT_1_ACTIONS", janitor)] = ScriptedAction(ActionType.CLEAN, victim)
    actions[("NIGHT_1_ACTIONS", roleblocker)] = ScriptedAction(ActionType.ROLEBLOCK, doctor)
    actions[("NIGHT_1_ACTIONS", doctor)] = ScriptedAction(ActionType.PROTECT, victim)
    actions[("NIGHT_1_ACTIONS", detective)] = ScriptedAction(
        ActionType.INVESTIGATE,
        godfather,
    )
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_2_VOTE", target=roleblocker)
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_3_VOTE", target=janitor)
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_4_VOTE", target=godfather)

    return DeterministicMockAdapter(_script_from_actions(deception13_v1, seats, actions))


def _visit12_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-visit12-real", visit12_v1)
    seat_ids = _seat_ids(seats)
    mafia_goons = _role_ids(seats, Role.MAFIA_GOON)
    roleblocker = _role_id(seats, Role.MAFIA_ROLEBLOCKER)
    detective = _role_id(seats, Role.DETECTIVE)
    doctor = _role_id(seats, Role.DOCTOR)
    tracker = _role_id(seats, Role.TRACKER)
    watcher = _role_id(seats, Role.WATCHER)
    victim = _first_villager(seats)
    actions: dict[tuple[str, str], ScriptedAction] = {}

    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=roleblocker)
    actions[("NIGHT_1_ACTIONS", mafia_goons[0])] = ScriptedAction(
        ActionType.MAFIA_KILL,
        victim,
    )
    actions[("NIGHT_1_ACTIONS", tracker)] = ScriptedAction(ActionType.TRACK, mafia_goons[0])
    actions[("NIGHT_1_ACTIONS", watcher)] = ScriptedAction(ActionType.WATCH, victim)
    actions[("NIGHT_1_ACTIONS", doctor)] = ScriptedAction(ActionType.PROTECT, tracker)
    actions[("NIGHT_1_ACTIONS", detective)] = ScriptedAction(
        ActionType.INVESTIGATE,
        mafia_goons[0],
    )
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_2_VOTE", target=mafia_goons[0])
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_3_VOTE", target=mafia_goons[1])

    return DeterministicMockAdapter(_script_from_actions(visit12_v1, seats, actions))


def _ninja13_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-ninja13-real", ninja13_v1)
    seat_ids = _seat_ids(seats)
    ninja = _role_id(seats, Role.NINJA)
    mafia_goon = _role_id(seats, Role.MAFIA_GOON)
    roleblocker = _role_id(seats, Role.MAFIA_ROLEBLOCKER)
    detective = _role_id(seats, Role.DETECTIVE)
    doctor = _role_id(seats, Role.DOCTOR)
    tracker = _role_id(seats, Role.TRACKER)
    watcher = _role_id(seats, Role.WATCHER)
    victim = _first_villager(seats)
    actions: dict[tuple[str, str], ScriptedAction] = {}

    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=roleblocker)
    actions[("NIGHT_1_ACTIONS", ninja)] = ScriptedAction(ActionType.MAFIA_KILL, victim)
    actions[("NIGHT_1_ACTIONS", tracker)] = ScriptedAction(ActionType.TRACK, ninja)
    actions[("NIGHT_1_ACTIONS", watcher)] = ScriptedAction(ActionType.WATCH, victim)
    actions[("NIGHT_1_ACTIONS", doctor)] = ScriptedAction(ActionType.PROTECT, tracker)
    actions[("NIGHT_1_ACTIONS", detective)] = ScriptedAction(ActionType.INVESTIGATE, ninja)
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_2_VOTE", target=mafia_goon)
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_3_VOTE", target=ninja)

    return DeterministicMockAdapter(_script_from_actions(ninja13_v1, seats, actions))


def _sk12_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-sk12-real", sk12_v1)
    seat_ids = _seat_ids(seats)
    mafia_goons = _role_ids(seats, Role.MAFIA_GOON)
    serial_killer = _role_id(seats, Role.SERIAL_KILLER)
    victim = _first_villager(seats)
    actions: dict[tuple[str, str], ScriptedAction] = {}

    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=mafia_goons[0])
    actions[("NIGHT_1_ACTIONS", mafia_goons[1])] = ScriptedAction(
        ActionType.MAFIA_KILL,
        victim,
    )
    actions[("NIGHT_1_ACTIONS", serial_killer)] = ScriptedAction(
        ActionType.SERIAL_KILL,
        mafia_goons[1],
    )
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_2_VOTE", target=mafia_goons[2])
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_3_VOTE", target=serial_killer)

    return DeterministicMockAdapter(_script_from_actions(sk12_v1, seats, actions))


def _jester8_adapter() -> DeterministicMockAdapter:
    seats = assign_roles("nar-corpus-jester8-real", jester8_v1)
    seat_ids = _seat_ids(seats)
    jester = _role_id(seats, Role.JESTER)
    actions: dict[tuple[str, str], ScriptedAction] = {}
    _vote_out(actions, seat_ids=seat_ids, phase_id="DAY_1_VOTE", target=jester)

    return DeterministicMockAdapter(_script_from_actions(jester8_v1, seats, actions))


def _assert_mini7_town_actions(outcome: GameOutcome) -> None:
    assert _payloads(outcome, "ProtectSubmitted")
    assert _payloads(outcome, "InvestigateSubmitted")
    assert _payloads(outcome, "MafiaKillVoteSubmitted")


def _assert_bench10_draw(outcome: GameOutcome) -> None:
    assert not _payloads(outcome, "PlayerEliminated")


def _assert_roleblock10_real_action(outcome: GameOutcome) -> None:
    feedback = _payloads(outcome, "NightFeedbackDelivered")
    assert _payloads(outcome, "RoleblockSubmitted")
    assert any(payload.get("code") == "ACTION_BLOCKED" for payload in feedback)
    assert not _payloads(outcome, "DetectiveResultDelivered")


def _assert_deception13_real_action(outcome: GameOutcome) -> None:
    night_resolved = _payloads(outcome, "NightResolved")
    cleaned_deaths = tuple(
        target for payload in night_resolved for target in payload.get("cleaned_deaths", ())
    )
    deaths = _payloads(outcome, "PlayerEliminated")
    godfather = _role_id(
        assign_roles("nar-corpus-deception13-real", deception13_v1), Role.GODFATHER
    )
    assert _payloads(outcome, "CleanSubmitted")
    assert cleaned_deaths
    assert any(
        payload.get("public_player_id") in cleaned_deaths
        and "role" not in payload
        and "faction" not in payload
        for payload in deaths
    )
    assert any(
        payload.get("target") == godfather and payload.get("finding") == "TOWN"
        for payload in _payloads(outcome, "DetectiveResultDelivered")
    )


def _assert_visit12_real_action(outcome: GameOutcome) -> None:
    feedback = _payloads(outcome, "NightFeedbackDelivered")
    assert _payloads(outcome, "TrackSubmitted")
    assert _payloads(outcome, "WatchSubmitted")
    assert any(
        payload.get("code") == "TRACK_RESULT" and payload.get("visited_player_ids")
        for payload in feedback
    )
    assert any(
        payload.get("code") == "WATCH_RESULT" and payload.get("visitor_player_ids")
        for payload in feedback
    )


def _assert_ninja13_real_action(outcome: GameOutcome) -> None:
    ninja = _role_id(assign_roles("nar-corpus-ninja13-real", ninja13_v1), Role.NINJA)
    track_payload = next(
        payload
        for payload in _payloads(outcome, "NightFeedbackDelivered")
        if payload.get("code") == "TRACK_RESULT" and payload.get("target") == ninja
    )
    assert _payloads(outcome, "MafiaKillVoteSubmitted")
    assert _payloads(outcome, "TrackSubmitted")
    assert tuple(track_payload.get("visited_player_ids", ())) == ()


def _assert_sk12_real_action(outcome: GameOutcome) -> None:
    serial_payloads = _payloads(outcome, "SerialKillSubmitted")
    night_payloads = _payloads(outcome, "NightResolved")
    assert serial_payloads
    assert any(payload.get("serial_kill_target") for payload in night_payloads)
    assert any(
        len(tuple(payload.get("eliminated_player_ids", ()))) > 1 for payload in night_payloads
    )


def _assert_jester8_real_action(outcome: GameOutcome) -> None:
    jester = _role_id(assign_roles("nar-corpus-jester8-real", jester8_v1), Role.JESTER)
    deaths = _payloads(outcome, "PlayerEliminated")
    assert any(payload.get("target") == jester for payload in _payloads(outcome, "VoteSubmitted"))
    assert any(
        payload.get("public_player_id") == jester
        and payload.get("role") == Role.JESTER.value
        and payload.get("cause") == "day_vote"
        for payload in deaths
    )


CANONICAL_BASELINE_CASES: tuple[_CorpusCase, ...] = (
    _CorpusCase(
        ruleset_id=mini7_v1.RULESET_ID,
        case_id="mini7_town",
        game_id="G-NAR-MINI7-TOWN",
        seed="nar-corpus-mini7-town",
        adapter_factory=_mini7_town_win_adapter,
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        event_count=47,
        final_event_hash="4333c53b496bde926c457944520dbf1c713f6711489928fb63ddd40a091701bb",
        event_hash_chain_sha256="df7d38afe39c6867736bb872b07765f8ff7d1f25eaed3053fdfaafbfc4d4d616",
        assertion=_assert_mini7_town_actions,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=bench10_v1.RULESET_ID,
        case_id="bench10_draw",
        game_id="G-NAR-BENCH10-DRAW",
        seed="nar-corpus-bench10-draw",
        adapter_factory=_bench10_draw_adapter,
        terminal_result="DRAW",
        terminal_reason="MAX_DAYS_REACHED",
        event_count=125,
        final_event_hash="c241612e044bbfa75e346e61f7da3d6e31176f95ac95d32eb92d971770b0aa5f",
        event_hash_chain_sha256="918b471f1048def56fb8ee4d6333ae00e0fb72364c2768d8294cacc76343ac71",
        assertion=_assert_bench10_draw,
        real_action=False,
    ),
)

WAVE10_REAL_ACTION_CASES: tuple[_CorpusCase, ...] = (
    _CorpusCase(
        ruleset_id=roleblock10_v1.RULESET_ID,
        case_id="roleblock10_real",
        game_id="G-CORPUS-ROLEBLOCK10-REAL",
        seed="nar-corpus-roleblock10-real",
        adapter_factory=_roleblock10_adapter,
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        event_count=77,
        final_event_hash="32493ccbb2c03ff66ba79f35fa5425ed8580ec5767940a03071b2fb6b9420329",
        event_hash_chain_sha256="9d5c731cc18e1e4fa1f31f57d866585fe02b8954262d0075ec37a1a9530a5ec1",
        assertion=_assert_roleblock10_real_action,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=deception13_v1.RULESET_ID,
        case_id="deception13_real",
        game_id="G-CORPUS-DECEPTION13-REAL",
        seed="nar-corpus-deception13-real",
        adapter_factory=_deception13_adapter,
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        event_count=110,
        final_event_hash="123da3c60b85adcb3359d4ea402b82930553d67a3d7b7219dd14f36eee17d6d0",
        event_hash_chain_sha256="fafe83da63ea95d5cb9a06609914f0d1f0a042aae64171c32d46750ad56e15d2",
        assertion=_assert_deception13_real_action,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=visit12_v1.RULESET_ID,
        case_id="visit12_real",
        game_id="G-CORPUS-VISIT12-REAL",
        seed="nar-corpus-visit12-real",
        adapter_factory=_visit12_adapter,
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        event_count=87,
        final_event_hash="2ec727783a2da3f04ef1e37883bcf44967043ebc472580888fed18812882d6cf",
        event_hash_chain_sha256="9c59a25b17a0f4f0335f980d03cb9669ab79585aa60b69ea3e82a70c75465348",
        assertion=_assert_visit12_real_action,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=ninja13_v1.RULESET_ID,
        case_id="ninja13_real",
        game_id="G-CORPUS-NINJA13-REAL",
        seed="nar-corpus-ninja13-real",
        adapter_factory=_ninja13_adapter,
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        event_count=90,
        final_event_hash="c3647e09aa8cba50a7e1163314db54d16dcb9ec30202ac831c957253fba8c897",
        event_hash_chain_sha256="0f8f8272f3f154b5bba6010e95e8f7893ccb643fbb2b8c8f793cbe6dd9436aff",
        assertion=_assert_ninja13_real_action,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=sk12_v1.RULESET_ID,
        case_id="sk12_real",
        game_id="G-CORPUS-SK12-REAL",
        seed="nar-corpus-sk12-real",
        adapter_factory=_sk12_adapter,
        terminal_result=Faction.TOWN.value,
        terminal_reason=sk12_v1.REASON_ALL_THREATS_ELIMINATED,
        event_count=80,
        final_event_hash="37a52e215ab8a295538c54b72bf7b89cb8e8fdb97e12b123dc032640f055fffe",
        event_hash_chain_sha256="58ac9d30d4f744049874a46a65b5bf64d6592f5cf81c33a5ded58caec3b74b01",
        assertion=_assert_sk12_real_action,
        real_action=True,
    ),
    _CorpusCase(
        ruleset_id=jester8_v1.RULESET_ID,
        case_id="jester8_real",
        game_id="G-CORPUS-JESTER8-REAL",
        seed="nar-corpus-jester8-real",
        adapter_factory=_jester8_adapter,
        terminal_result=jester8_v1.JESTER_WINNER,
        terminal_reason=jester8_v1.REASON_JESTER_DAY_VOTED_OUT,
        event_count=23,
        final_event_hash="0b12bb702339755f51722a7b32e5485d47f96353ba33d0a62af3d36624192a4e",
        event_hash_chain_sha256="e5e7748edf54e952a403f3ea17d656557d2fee0da3896fac38229183e3e083d1",
        assertion=_assert_jester8_real_action,
        real_action=True,
    ),
)

CORPUS_CASES: tuple[_CorpusCase, ...] = CANONICAL_BASELINE_CASES + WAVE10_REAL_ACTION_CASES


async def _run_case(case: _CorpusCase) -> GameOutcome:
    return await run_game(
        GameConfig(
            game_id=case.game_id,
            game_seed=case.seed,
            ruleset_id=case.ruleset_id,
            timeout_s=1.0,
        ),
        case.adapter_factory(),
        ranked=False,
    )


def _assert_replay_matches(outcome: GameOutcome) -> None:
    replayed_log = replay_event_log(outcome.event_log.events)
    typed_events = tuple(
        EventAdapter.validate_python(stored.body) for stored in outcome.event_log.events
    )
    replayed_state = replay_events(typed_events)
    assert replayed_state == outcome.final_state
    assert replayed_log.events == outcome.event_log.events


@pytest.mark.parametrize("case", CORPUS_CASES, ids=lambda case: case.case_id)
async def test_nar_refactor_preserves_pinned_event_hash_corpus(case: _CorpusCase) -> None:
    outcome = await _run_case(case)

    _assert_replay_matches(outcome)
    case.assertion(outcome)

    assert outcome.final_state.terminal_result == case.terminal_result
    assert outcome.final_state.terminal_reason == case.terminal_reason
    assert len(outcome.event_log.events) == case.event_count
    assert outcome.event_log.events[-1].event_hash == case.final_event_hash
    assert _event_hash_chain_digest(outcome) == case.event_hash_chain_sha256


@pytest.mark.parametrize("case", WAVE10_REAL_ACTION_CASES, ids=lambda case: case.case_id)
async def test_wave10_real_action_goldens_are_stable_across_rerun(
    case: _CorpusCase,
) -> None:
    first = await _run_case(case)
    second = await _run_case(case)

    _assert_replay_matches(first)
    _assert_replay_matches(second)
    case.assertion(first)
    case.assertion(second)

    assert case.real_action is True
    assert first.final_state.terminal_result == second.final_state.terminal_result
    assert first.final_state.terminal_result == case.terminal_result
    assert first.event_log.events[-1].event_hash == second.event_log.events[-1].event_hash
    assert first.event_log.events[-1].event_hash == case.final_event_hash
    assert _event_hash_chain_digest(first) == _event_hash_chain_digest(second)
    assert _event_hash_chain_digest(first) == case.event_hash_chain_sha256


def test_wave10_real_action_corpus_covers_every_wave10_ruleset() -> None:
    assert {case.ruleset_id for case in WAVE10_REAL_ACTION_CASES} == {
        "deception13_v1",
        "roleblock10_v1",
        "visit12_v1",
        "ninja13_v1",
        "sk12_v1",
        "jester8_v1",
    }
    assert all(case.real_action for case in WAVE10_REAL_ACTION_CASES)


def test_every_builtin_canonical_ruleset_has_a_pinned_corpus_hash() -> None:
    canonical_ids = {
        ruleset_id
        for ruleset_id in BUILTIN_RULESET_IDS
        if get_ruleset(ruleset_id).IS_CANONICAL is True
    }
    pinned_ids = {
        case.ruleset_id for case in CORPUS_CASES if get_ruleset(case.ruleset_id).IS_CANONICAL
    }

    assert pinned_ids == canonical_ids
