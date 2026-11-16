"""Full-game golden fixtures for sacred canonical byte stability."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.rulesets import bench10_v1, mini7_v1
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game


class _CanonicalRuleset(Protocol):
    RULESET_ID: str
    PLAYER_COUNT: int
    MAX_DAYS: int
    DISCUSSION_ROUNDS_PER_DAY: int
    ROLE_COUNTS: dict[Role, int]
    ROLE_FACTIONS: dict[Role, Faction]


@dataclass(frozen=True, slots=True)
class _GoldenCase:
    case_id: str
    ruleset: _CanonicalRuleset
    game_id: str
    seed: str
    terminal_result: str
    terminal_reason: str
    eliminated_sequence: tuple[str, ...]
    event_count: int
    final_event_hash: str
    event_hash_chain_sha256: str


GOLDEN_CASES: tuple[_GoldenCase, ...] = (
    _GoldenCase(
        case_id="mini7_a",
        ruleset=mini7_v1,
        game_id="G-CANON-BYTE-MINI7-A",
        seed="canonical-byte-mini7-a",
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        eliminated_sequence=("P01", "P03", "P06"),
        event_count=73,
        final_event_hash="6a1a8412ca194690a161aa66374747b8c2ef82d7e495423fbc7c72cc6be55958",
        event_hash_chain_sha256="3db888f257cd489f2d2da5a6ec87c10bf12002e5def9fed361445368df89bfe1",
    ),
    _GoldenCase(
        case_id="mini7_b",
        ruleset=mini7_v1,
        game_id="G-CANON-BYTE-MINI7-B",
        seed="canonical-byte-mini7-b",
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        eliminated_sequence=("P01", "P05", "P03"),
        event_count=73,
        final_event_hash="825e4bef715e76a13eadb9023a47bbaba1857b5e633f8f488912cae7621a4d8c",
        event_hash_chain_sha256="cd17a454d8b1110dc3facea7ce2acafacb4aaa7796c940759326e03878e45cd6",
    ),
    _GoldenCase(
        case_id="bench10_a",
        ruleset=bench10_v1,
        game_id="G-CANON-BYTE-BENCH10-A",
        seed="canonical-byte-bench10-a",
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        eliminated_sequence=("P02", "P03", "P07", "P04", "P08"),
        event_count=110,
        final_event_hash="f18116db1696ef0a7747a3d5ebbe2bc7375ecd9c8b0c9f303d12b029b4d8be4c",
        event_hash_chain_sha256="20bb6325a3455469569c6c222a62e5aa1eefb1e5dfe7686a8307ed9911af2f48",
    ),
    _GoldenCase(
        case_id="bench10_b",
        ruleset=bench10_v1,
        game_id="G-CANON-BYTE-BENCH10-B",
        seed="canonical-byte-bench10-b",
        terminal_result="TOWN",
        terminal_reason="ALL_MAFIA_ELIMINATED",
        eliminated_sequence=("P01", "P05", "P04", "P06", "P09"),
        event_count=110,
        final_event_hash="5aec8a45da6d954d3c573c2390949992aaaa1e3197f379048ce90b864eff9565",
        event_hash_chain_sha256="b1d341767a655e2c9f47826a88acb711fe064e2c60c49365c15229641fc6d7c0",
    ),
)


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _phase_default(phase_id: str) -> AgentResponse:
    if phase_id.endswith("_VOTE"):
        return _response(ActionType.ABSTAIN)
    return _response(ActionType.NOOP)


def _phase_ids(ruleset: _CanonicalRuleset) -> tuple[str, ...]:
    out: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for day in range(1, ruleset.MAX_DAYS + 1):
        for round_index in range(1, ruleset.DISCUSSION_ROUNDS_PER_DAY + 1):
            out.append(f"DAY_{day}_DISCUSSION_ROUND_{round_index}")
        out.append(f"DAY_{day}_VOTE")
        out.append(f"NIGHT_{day}_MAFIA_DISCUSSION")
        out.append(f"NIGHT_{day}_ACTIONS")
    return tuple(out)


def _first_living_protect_alternative(
    candidates: Sequence[str],
    *,
    previous_protect_target: str,
    kill_target: str,
) -> str:
    for candidate in candidates:
        if candidate not in (previous_protect_target, kill_target):
            return candidate
    raise AssertionError("canonical fixture needs an alternate protect target")


def _script_for(case: _GoldenCase) -> dict[tuple[str, str], AgentResponse]:
    seats = assign_roles(case.seed, case.ruleset)
    mafia_ids = tuple(seat.public_player_id for seat in seats if seat.faction is Faction.MAFIA)
    town_ids = tuple(seat.public_player_id for seat in seats if seat.faction is Faction.TOWN)
    detective_id = next(seat.public_player_id for seat in seats if seat.role is Role.DETECTIVE)
    doctor_id = next(seat.public_player_id for seat in seats if seat.role is Role.DOCTOR)
    villager_ids = tuple(seat.public_player_id for seat in seats if seat.role is Role.VILLAGER)
    if len(villager_ids) < len(mafia_ids):
        raise AssertionError("canonical fixture needs one villager kill target per mafia")

    phase_ids = _phase_ids(case.ruleset)
    all_seats = tuple(seat.public_player_id for seat in seats)
    script: dict[tuple[str, str], AgentResponse] = {
        (phase_id, seat_id): _phase_default(phase_id)
        for phase_id in phase_ids
        for seat_id in all_seats
    }

    previous_protect_target = ""
    protect_candidates = (detective_id, doctor_id, *town_ids)
    for index, mafia_id in enumerate(mafia_ids):
        night_phase = f"NIGHT_{index + 1}_ACTIONS"
        kill_target = villager_ids[index]
        protect_target = (
            kill_target
            if index == 0
            else _first_living_protect_alternative(
                protect_candidates,
                previous_protect_target=previous_protect_target,
                kill_target=kill_target,
            )
        )
        previous_protect_target = protect_target

        for actor_id in mafia_ids:
            script[(night_phase, actor_id)] = _response(ActionType.MAFIA_KILL, kill_target)
        script[(night_phase, doctor_id)] = _response(ActionType.PROTECT, protect_target)
        script[(night_phase, detective_id)] = _response(ActionType.INVESTIGATE, mafia_id)

        vote_phase = f"DAY_{index + 2}_VOTE"
        for voter_id in all_seats:
            if voter_id != mafia_id:
                script[(vote_phase, voter_id)] = _response(ActionType.VOTE, mafia_id)

    return script


async def _run_case(case: _GoldenCase) -> GameOutcome:
    return await run_game(
        GameConfig(
            game_id=case.game_id,
            game_seed=case.seed,
            ruleset_id=case.ruleset.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(_script_for(case)),
        ranked=False,
    )


def _event_hash_chain_digest(outcome: GameOutcome) -> str:
    joined = "\n".join(stored.event_hash for stored in outcome.event_log.events)
    return hashlib.sha256(joined.encode()).hexdigest()


def _payload_targets(outcome: GameOutcome, event_type: str) -> tuple[str, ...]:
    targets: list[str] = []
    for stored in outcome.event_log.events:
        body = stored.body
        if body["event_type"] != event_type:
            continue
        target = body["payload"].get("target")
        if isinstance(target, str):
            targets.append(target)
    return tuple(targets)


def _unique_targets(outcome: GameOutcome, event_type: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_payload_targets(outcome, event_type)))


def _eliminated_sequence(outcome: GameOutcome) -> tuple[str, ...]:
    eliminated: list[str] = []
    for stored in outcome.event_log.events:
        body = stored.body
        if body["event_type"] != "PlayerEliminated":
            continue
        public_player_id = body["payload"].get("public_player_id")
        if isinstance(public_player_id, str):
            eliminated.append(public_player_id)
    return tuple(eliminated)


def _mafia_ids(case: _GoldenCase) -> tuple[str, ...]:
    return tuple(
        seat.public_player_id
        for seat in assign_roles(case.seed, case.ruleset)
        if seat.faction is Faction.MAFIA
    )


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.case_id)
async def test_canonical_full_games_with_real_night_actions_are_byte_stable(
    case: _GoldenCase,
) -> None:
    outcome = await _run_case(case)

    assert _unique_targets(outcome, "InvestigateSubmitted") == _mafia_ids(case)
    assert _unique_targets(outcome, "DetectiveResultDelivered") == _mafia_ids(case)
    assert _payload_targets(outcome, "ProtectSubmitted")
    assert _payload_targets(outcome, "MafiaKillVoteSubmitted")

    replayed_log = replay_event_log(outcome.event_log.events)
    typed_events = tuple(
        EventAdapter.validate_python(stored.body) for stored in outcome.event_log.events
    )
    replayed_state = replay_events(typed_events)
    assert replayed_state == outcome.final_state
    assert replayed_log.events == outcome.event_log.events

    assert outcome.final_state.terminal_result == case.terminal_result
    assert outcome.final_state.terminal_reason == case.terminal_reason
    assert _eliminated_sequence(outcome) == case.eliminated_sequence
    assert len(outcome.event_log.events) == case.event_count
    assert outcome.event_log.events[-1].event_hash == case.final_event_hash
    assert _event_hash_chain_digest(outcome) == case.event_hash_chain_sha256
