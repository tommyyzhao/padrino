"""Pure casual cadence profile tests (US-338)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from padrino.core.cadence import (
    CASUAL_FAST_CADENCE_ID,
    CASUAL_RELAXED_CADENCE_ID,
    CASUAL_STANDARD_CADENCE_ID,
    PLAYER_SELECTABLE_CADENCE_IDS,
    cadence_profile,
)
from padrino.core.enums import PhaseKind
from padrino.core.rulesets import mini7_v1


def test_standard_casual_cadence_maps_phase_and_discussion_round() -> None:
    profile = cadence_profile(CASUAL_STANDARD_CADENCE_ID)

    first = profile.timing_for(PhaseKind.DAY_DISCUSSION, round_number=1)
    third = profile.timing_for(PhaseKind.DAY_DISCUSSION, round_number=3)
    vote = profile.timing_for(PhaseKind.DAY_VOTE)
    night = profile.timing_for(PhaseKind.NIGHT_ACTIONS)

    assert first.deadline_seconds == 75
    assert first.release_delay_seconds == 3
    assert first.countdown_label == "Discuss · Round 1"
    assert third.deadline_seconds == 45
    assert third.countdown_label == "Discuss · Round 3"
    assert vote.countdown_label == "Cast your vote"
    assert night.countdown_label == "Choose your night action"


def test_standard_max_day_schedule_targets_twenty_to_thirty_minutes() -> None:
    profile = cadence_profile(CASUAL_STANDARD_CADENCE_ID)

    scheduled_seconds = profile.maximum_game_seconds(
        max_days=mini7_v1.MAX_DAYS,
        discussion_rounds_per_day=mini7_v1.DISCUSSION_ROUNDS_PER_DAY,
    )

    assert 20 * 60 <= scheduled_seconds <= 30 * 60
    assert scheduled_seconds == 1_515


@pytest.mark.parametrize(
    "cadence_id",
    [CASUAL_FAST_CADENCE_ID, CASUAL_STANDARD_CADENCE_ID, CASUAL_RELAXED_CADENCE_ID],
)
def test_all_casual_presets_are_player_selectable_and_complete(cadence_id: str) -> None:
    profile = cadence_profile(cadence_id)

    assert cadence_id in PLAYER_SELECTABLE_CADENCE_IDS
    for phase_kind, round_number in (
        (PhaseKind.NIGHT_0_MAFIA_INTRO, 0),
        (PhaseKind.DAY_DISCUSSION, 1),
        (PhaseKind.DAY_DISCUSSION, 2),
        (PhaseKind.DAY_DISCUSSION, 3),
        (PhaseKind.DAY_VOTE, 0),
        (PhaseKind.NIGHT_MAFIA_DISCUSSION, 0),
        (PhaseKind.NIGHT_ACTIONS, 0),
    ):
        timing = profile.timing_for(phase_kind, round_number=round_number)
        assert timing.deadline_seconds > 0
        assert timing.release_delay_seconds >= 0
        assert timing.countdown_label


def test_cadence_profiles_are_frozen_and_reject_invalid_phase_requests() -> None:
    profile = cadence_profile(CASUAL_STANDARD_CADENCE_ID)

    with pytest.raises(FrozenInstanceError):
        profile.cadence_id = CASUAL_FAST_CADENCE_ID  # type: ignore[misc]
    with pytest.raises(ValueError, match="discussion_round"):
        profile.timing_for(PhaseKind.DAY_DISCUSSION, round_number=4)
    with pytest.raises(ValueError, match="non_playable_phase"):
        profile.timing_for(PhaseKind.SETUP)
    with pytest.raises(ValueError, match="unknown_cadence"):
        cadence_profile("ranked_player_selected")
