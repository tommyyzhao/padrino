"""Tests for engine-stress utility roles."""

from __future__ import annotations

from fractions import Fraction

from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import (
    NightFeedbackDelivered,
    NightFeedbackDeliveredPayload,
)
from padrino.core.engine.resolvers.day_vote import resolve_day_vote
from padrino.core.engine.resolvers.nar import (
    NightActionIntent,
    NightActionKind,
    resolve_night_actions,
)
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observation_privacy import assert_no_identity_markers
from padrino.core.observations import build_observation
from padrino.core.rulesets import BUILTIN_RULESET_IDS, get_ruleset, mini7_v1
from padrino.core.rulesets.mayor_variance_gate import (
    CURRENT_MAYOR_VARIANCE_GATE,
    evaluate_mayor_variance_gate,
)


def _seat(pid: str, idx: int, role: Role, faction: Faction, *, alive: bool = True) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
    )


def _state(
    *,
    phase: Phase | None = None,
    seats: tuple[Seat, ...] | None = None,
) -> GameState:
    return GameState(
        ruleset_id="utility-role-test",
        game_id="G-UTILITY",
        game_seed="utility-seed",
        current_phase=phase or Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        seats=seats
        or (
            _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
            _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
            _seat("P03", 2, Role.MAYOR, Faction.TOWN),
            _seat("P04", 3, Role.COMMUTER, Faction.TOWN),
            _seat("P05", 4, Role.MASON, Faction.TOWN),
            _seat("P06", 5, Role.MASON, Faction.TOWN),
            _seat("P07", 6, Role.DETECTIVE, Faction.TOWN),
        ),
        day=1,
    )


def _intent(actor: str, kind: NightActionKind, target: str | None) -> NightActionIntent:
    return NightActionIntent(actor=actor, kind=kind, target=target)


def test_mayor_vote_weight_drives_public_tally_and_threshold_math() -> None:
    state = _state()

    result = resolve_day_vote(
        state,
        {
            "P01": Action(type=ActionType.VOTE, target="P04"),
            "P02": Action(type=ActionType.VOTE, target="P04"),
            "P03": Action(type=ActionType.VOTE, target="P01"),
            "P05": Action(type=ActionType.VOTE, target="P01"),
        },
    )

    assert result.vote_tally == {"P04": 2, "P01": 3}
    assert result.voter_weights == {"P01": 1, "P02": 1, "P03": 2, "P05": 1}
    assert result.total_vote_weight == 8
    assert result.hammer_threshold == 5
    assert result.eliminated == "P01"
    assert_no_identity_markers(result.model_dump(mode="json"))


def test_mayor_is_not_admitted_to_canonical_without_gate() -> None:
    assert CURRENT_MAYOR_VARIANCE_GATE.enabled is False
    assert (
        evaluate_mayor_variance_gate(
            mirror_paired_games=200,
            weighted_vote_rng_variance=Fraction(1, 100),
            measured_skill_delta=Fraction(99, 100),
        ).enabled
        is True
    )

    for ruleset_id in BUILTIN_RULESET_IDS:
        ruleset = get_ruleset(ruleset_id)
        if ruleset.IS_CANONICAL:
            assert Role.MAYOR not in ruleset.ROLE_COUNTS


def test_commuter_bounces_targeted_night_actions_at_nar_tier() -> None:
    state = _state(phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))

    result = resolve_night_actions(
        state,
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P04"),
            _intent("P02", NightActionKind.FACTIONAL_KILL, "P04"),
            _intent("P07", NightActionKind.INVESTIGATE, "P04"),
        ),
    )

    assert result.mafia_kill_target is None
    assert result.eliminated is None
    assert result.detective_finding is None
    assert result.commuter_bounced_targets == ("P04",)
    feedback = result.feedback_by_code("COMMUTER_UNTARGETABLE")
    assert [(f.recipient, f.target) for f in feedback] == [
        ("P01", "P04"),
        ("P02", "P04"),
        ("P07", "P04"),
    ]
    assert_no_identity_markers(result.model_dump(mode="json"))


def test_mason_shared_fact_is_structured_public_ids_only() -> None:
    state = _state(phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))

    mason = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    partner = build_observation(state, state.seats[5], EventLog(), mini7_v1)
    outsider = build_observation(state, state.seats[6], EventLog(), mini7_v1)

    assert mason.mason_partners == ("P06",)
    assert partner.mason_partners == ("P05",)
    assert outsider.mason_partners is None
    assert_no_identity_markers(mason.model_dump(mode="json"))


def test_commuter_feedback_reaches_only_targeting_actor_observation() -> None:
    state = _state(phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=1))
    log = EventLog()
    log.append(
        NightFeedbackDelivered(
            sequence=0,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P01",
            payload=NightFeedbackDeliveredPayload(code="COMMUTER_UNTARGETABLE", target="P04"),
        ).model_dump(mode="json")
    )

    actor = build_observation(state, state.seats[0], log, mini7_v1)
    commuter = build_observation(state, state.seats[3], log, mini7_v1)

    assert [entry.model_dump(mode="json") for entry in actor.role_feedback] == [
        {
            "code": "COMMUTER_UNTARGETABLE",
            "phase": "NIGHT_1_ACTIONS",
            "target": "P04",
            "finding": None,
            "visited_player_ids": [],
            "visitor_player_ids": [],
        }
    ]
    assert commuter.role_feedback == ()
