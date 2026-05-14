"""Tests for the phase-sequence FSM."""

from __future__ import annotations

import pytest

from padrino.core.engine.phases import next_phase
from padrino.core.engine.state import Phase
from padrino.core.enums import PhaseKind
from padrino.core.rulesets import mini7_v1


def test_setup_advances_to_night_zero_intro() -> None:
    nxt = next_phase(Phase(kind=PhaseKind.SETUP, day=0, round=0), mini7_v1)
    assert nxt == Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0)


def test_night_zero_intro_advances_to_day_one_discussion_round_one() -> None:
    nxt = next_phase(
        Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0),
        mini7_v1,
    )
    assert nxt == Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1)


def test_discussion_rounds_advance_within_day() -> None:
    nxt = next_phase(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1), mini7_v1)
    assert nxt == Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=2)
    nxt = next_phase(nxt, mini7_v1)
    assert nxt == Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=3)


def test_final_discussion_round_advances_to_vote() -> None:
    nxt = next_phase(Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=3), mini7_v1)
    assert nxt == Phase(kind=PhaseKind.DAY_VOTE, day=2, round=0)


def test_day_vote_advances_to_night_mafia_discussion() -> None:
    nxt = next_phase(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0), mini7_v1)
    assert nxt == Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0)


def test_night_mafia_discussion_advances_to_night_actions() -> None:
    nxt = next_phase(
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=2, round=0),
        mini7_v1,
    )
    assert nxt == Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)


def test_night_actions_before_max_days_advances_to_next_day_discussion_one() -> None:
    nxt = next_phase(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0), mini7_v1)
    assert nxt == Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=1)


def test_night_actions_at_max_days_advances_to_terminal() -> None:
    nxt = next_phase(
        Phase(kind=PhaseKind.NIGHT_ACTIONS, day=mini7_v1.MAX_DAYS, round=0),
        mini7_v1,
    )
    assert nxt == Phase(kind=PhaseKind.TERMINAL, day=mini7_v1.MAX_DAYS, round=0)


def test_terminal_has_no_successor() -> None:
    with pytest.raises(ValueError, match="TERMINAL"):
        next_phase(Phase(kind=PhaseKind.TERMINAL, day=5, round=0), mini7_v1)


def test_invalid_discussion_round_raises() -> None:
    with pytest.raises(ValueError, match="discussion round"):
        next_phase(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=0), mini7_v1)
    with pytest.raises(ValueError, match="discussion round"):
        next_phase(
            Phase(
                kind=PhaseKind.DAY_DISCUSSION, day=1, round=mini7_v1.DISCUSSION_ROUNDS_PER_DAY + 1
            ),
            mini7_v1,
        )


def test_full_sequence_enumeration_terminates_at_max_days() -> None:
    """Walking from SETUP must terminate without exceeding MAX_DAYS."""
    expected: list[Phase] = [Phase(kind=PhaseKind.SETUP, day=0, round=0)]
    expected.append(Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))
    for d in range(1, mini7_v1.MAX_DAYS + 1):
        for r in range(1, mini7_v1.DISCUSSION_ROUNDS_PER_DAY + 1):
            expected.append(Phase(kind=PhaseKind.DAY_DISCUSSION, day=d, round=r))
        expected.append(Phase(kind=PhaseKind.DAY_VOTE, day=d, round=0))
        expected.append(Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=d, round=0))
        expected.append(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=d, round=0))
    expected.append(Phase(kind=PhaseKind.TERMINAL, day=mini7_v1.MAX_DAYS, round=0))

    walked: list[Phase] = [expected[0]]
    while walked[-1].kind is not PhaseKind.TERMINAL:
        walked.append(next_phase(walked[-1], mini7_v1))
        assert walked[-1].day <= mini7_v1.MAX_DAYS

    assert walked == expected
