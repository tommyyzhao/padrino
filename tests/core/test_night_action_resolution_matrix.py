"""Tests for the formal Night Action Resolution matrix."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from padrino.core.engine.resolvers.nar import (
    RESOLUTION_MATRIX,
    TIER_ORDER,
    MatrixEffect,
    NarTier,
    NightActionIntent,
    NightActionKind,
    resolve_night_actions,
)
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role


def _seat(
    pid: str,
    idx: int,
    role: Role,
    faction: Faction,
    *,
    alive: bool = True,
    last_protected_target: str | None = None,
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
        last_protected_target=last_protected_target,
    )


def _state() -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-NAR",
        game_seed="nar-seed",
        current_phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        seats=(
            _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
            _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
            _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
            _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
            _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
            _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
            _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
        ),
        day=1,
    )


def _intent(
    actor: str,
    kind: NightActionKind,
    target: str | None,
    *,
    redirect_target: str | None = None,
) -> NightActionIntent:
    return NightActionIntent(
        actor=actor,
        kind=kind,
        target=target,
        redirect_target=redirect_target,
    )


def test_resolution_matrix_declares_tier_order_and_rows() -> None:
    assert TIER_ORDER == (
        NarTier.ROLEBLOCK,
        NarTier.REDIRECT,
        NarTier.KILL_PROTECT_VISIT,
        NarTier.INVESTIGATION,
        NarTier.DEATH_REVEAL,
    )
    assert set(RESOLUTION_MATRIX) == set(NightActionKind)
    assert RESOLUTION_MATRIX[NightActionKind.ROLEBLOCK].tier is NarTier.ROLEBLOCK
    assert RESOLUTION_MATRIX[NightActionKind.REDIRECT].tier is NarTier.REDIRECT
    assert RESOLUTION_MATRIX[NightActionKind.FACTIONAL_KILL].tier is (NarTier.KILL_PROTECT_VISIT)
    assert RESOLUTION_MATRIX[NightActionKind.PROTECT].protected is (MatrixEffect.PREVENTS_DEATH)
    assert RESOLUTION_MATRIX[NightActionKind.INVESTIGATE].tier is NarTier.INVESTIGATION
    assert RESOLUTION_MATRIX[NightActionKind.FRAME].tier is NarTier.INVESTIGATION
    assert RESOLUTION_MATRIX[NightActionKind.TRACK].tier is NarTier.INVESTIGATION
    assert RESOLUTION_MATRIX[NightActionKind.WATCH].tier is NarTier.INVESTIGATION
    assert RESOLUTION_MATRIX[NightActionKind.CLEAN].tier is NarTier.DEATH_REVEAL


def test_roleblock_vs_roleblock_both_blocks_apply() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.ROLEBLOCK, "P02"),
            _intent("P02", NightActionKind.ROLEBLOCK, "P01"),
        ),
    )

    assert result.blocked_actor_ids == ("P01", "P02")
    assert {(v.actor, v.target, v.action_kind) for v in result.visits} == {
        ("P01", "P02", NightActionKind.ROLEBLOCK),
        ("P02", "P01", NightActionKind.ROLEBLOCK),
    }


def test_roleblocked_factional_killer_fails_without_backup_carry() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P04", NightActionKind.ROLEBLOCK, "P01"),
        ),
    )

    assert result.mafia_kill_target is None
    assert result.eliminated is None
    assert result.mafia_vote_tally == {}
    assert result.feedback_by_code("ACTION_BLOCKED")[0].recipient == "P01"


def test_blocked_action_does_not_count_as_visit_but_roleblock_does() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P04", NightActionKind.ROLEBLOCK, "P03"),
            _intent("P03", NightActionKind.INVESTIGATE, "P01"),
        ),
    )

    assert result.blocked_actor_ids == ("P03",)
    assert {(v.actor, v.target) for v in result.visits} == {("P04", "P03")}
    assert result.detective_finding is None


def test_investigation_of_target_killed_same_night_still_returns_result() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P02", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P03", NightActionKind.INVESTIGATE, "P05"),
        ),
    )

    assert result.mafia_kill_target == "P05"
    assert result.eliminated == "P05"
    assert result.detective_finding == ("P05", "TOWN")
    assert result.feedback_by_code("INVESTIGATION_RESULT")[0].message == (
        "Investigation result: P05 is TOWN."
    )


def test_protection_feedback_is_structured_and_deterministic() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P02", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P04", NightActionKind.PROTECT, "P05"),
        ),
    )

    assert result.protected == "P05"
    assert result.eliminated is None
    feedback = result.feedback_by_code("PROTECTION_SUCCESSFUL")
    assert [(f.recipient, f.target, f.message) for f in feedback] == [
        ("P04", "P05", "Your protection prevented a kill.")
    ]


def test_track_and_watch_feedback_use_public_player_ids_only() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P03", NightActionKind.TRACK, "P01"),
            _intent("P04", NightActionKind.WATCH, "P05"),
        ),
    )

    track = result.feedback_by_code("TRACK_RESULT")[0]
    watch = result.feedback_by_code("WATCH_RESULT")[0]
    assert track.recipient == "P03"
    assert track.target == "P01"
    assert track.visited_player_ids == ("P05",)
    assert track.visitor_player_ids == ()
    assert watch.recipient == "P04"
    assert watch.target == "P05"
    assert watch.visitor_player_ids == ("P01",)
    assert watch.visited_player_ids == ()


def test_cleaned_death_suppresses_death_reveal_only() -> None:
    result = resolve_night_actions(
        _state(),
        (
            _intent("P01", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P02", NightActionKind.FACTIONAL_KILL, "P05"),
            _intent("P06", NightActionKind.CLEAN, "P05"),
        ),
    )

    assert result.eliminated == "P05"
    assert result.cleaned_deaths == ("P05",)
    assert [(r.public_player_id, r.role, r.faction, r.cleaned) for r in result.death_reveals] == [
        ("P05", None, None, True)
    ]


_PID = st.sampled_from(("P01", "P02", "P03", "P04", "P05", "P06", "P07"))
_KIND = st.sampled_from(tuple(NightActionKind))


@given(
    st.lists(
        st.builds(
            NightActionIntent,
            actor=_PID,
            kind=_KIND,
            target=st.one_of(_PID, st.none()),
            redirect_target=st.one_of(_PID, st.none()),
        ),
        max_size=12,
    )
)
def test_matrix_resolution_is_deterministic_for_generated_interactions(
    intents: list[NightActionIntent],
) -> None:
    first = resolve_night_actions(_state(), tuple(intents))
    second = resolve_night_actions(_state(), tuple(intents))
    assert second == first
