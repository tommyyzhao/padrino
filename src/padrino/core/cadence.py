"""Pure phase-specific cadence profiles for casual human games."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from padrino.core.enums import PhaseKind

BUILTIN_CADENCE_ID: Final[str] = "builtin_sync_v1"
CASUAL_FAST_CADENCE_ID: Final[str] = "casual_fast_v1"
CASUAL_STANDARD_CADENCE_ID: Final[str] = "casual_standard_v1"
CASUAL_RELAXED_CADENCE_ID: Final[str] = "casual_relaxed_v1"

PLAYER_SELECTABLE_CADENCE_IDS: Final[frozenset[str]] = frozenset(
    {
        CASUAL_FAST_CADENCE_ID,
        CASUAL_STANDARD_CADENCE_ID,
        CASUAL_RELAXED_CADENCE_ID,
    }
)
SUPPORTED_CADENCE_IDS: Final[frozenset[str]] = frozenset(
    {*PLAYER_SELECTABLE_CADENCE_IDS, BUILTIN_CADENCE_ID}
)


@dataclass(frozen=True, slots=True)
class PhaseCadence:
    """Deadline, symmetric release delay, and player-facing countdown label."""

    deadline_seconds: int
    release_delay_seconds: int
    countdown_label: str

    @property
    def scheduled_seconds(self) -> int:
        """Return the maximum wall-clock budget for this phase."""
        return self.deadline_seconds + self.release_delay_seconds


@dataclass(frozen=True, slots=True)
class CadenceProfile:
    """Frozen timing policy for the existing bounded round-based engine."""

    cadence_id: str
    mafia_intro: PhaseCadence
    discussion_rounds: tuple[PhaseCadence, ...]
    day_vote: PhaseCadence
    mafia_discussion: PhaseCadence
    night_actions: PhaseCadence

    def timing_for(
        self,
        phase_kind: PhaseKind,
        *,
        round_number: int = 0,
    ) -> PhaseCadence:
        """Resolve timing for one playable phase kind and discussion round."""
        if phase_kind is PhaseKind.NIGHT_0_MAFIA_INTRO:
            return self.mafia_intro
        if phase_kind is PhaseKind.DAY_DISCUSSION:
            if round_number < 1 or round_number > len(self.discussion_rounds):
                raise ValueError(f"invalid_discussion_round:{round_number}")
            return self.discussion_rounds[round_number - 1]
        if phase_kind is PhaseKind.DAY_VOTE:
            return self.day_vote
        if phase_kind is PhaseKind.NIGHT_MAFIA_DISCUSSION:
            return self.mafia_discussion
        if phase_kind is PhaseKind.NIGHT_ACTIONS:
            return self.night_actions
        raise ValueError(f"non_playable_phase:{phase_kind.value}")

    def maximum_game_seconds(
        self,
        *,
        max_days: int,
        discussion_rounds_per_day: int,
    ) -> int:
        """Return the bounded maximum schedule through ``max_days``.

        The standard profile intentionally totals 1,515 seconds (25m15s) for
        the shipped three-round, five-day rulesets. This includes the symmetric
        release delay and therefore describes the player's actual wait budget.
        """
        if max_days < 1:
            raise ValueError("max_days_must_be_positive")
        if discussion_rounds_per_day < 1 or discussion_rounds_per_day > len(self.discussion_rounds):
            raise ValueError(f"unsupported_discussion_round_count:{discussion_rounds_per_day}")
        daily = sum(
            timing.scheduled_seconds
            for timing in self.discussion_rounds[:discussion_rounds_per_day]
        )
        daily += self.day_vote.scheduled_seconds
        daily += self.mafia_discussion.scheduled_seconds
        daily += self.night_actions.scheduled_seconds
        return self.mafia_intro.scheduled_seconds + max_days * daily


def _phase(deadline: int, release: int, label: str) -> PhaseCadence:
    return PhaseCadence(
        deadline_seconds=deadline,
        release_delay_seconds=release,
        countdown_label=label,
    )


_PROFILES: Final[dict[str, CadenceProfile]] = {
    BUILTIN_CADENCE_ID: CadenceProfile(
        cadence_id=BUILTIN_CADENCE_ID,
        mafia_intro=_phase(45, 0, "Meet your faction"),
        discussion_rounds=(
            _phase(45, 0, "Discuss · Round 1"),
            _phase(45, 0, "Discuss · Round 2"),
            _phase(45, 0, "Discuss · Round 3"),
        ),
        day_vote=_phase(45, 0, "Cast your vote"),
        mafia_discussion=_phase(45, 0, "Mafia discussion"),
        night_actions=_phase(45, 0, "Choose your night action"),
    ),
    CASUAL_FAST_CADENCE_ID: CadenceProfile(
        cadence_id=CASUAL_FAST_CADENCE_ID,
        mafia_intro=_phase(15, 0, "Meet your faction"),
        discussion_rounds=(
            _phase(30, 1, "Discuss · Round 1"),
            _phase(25, 1, "Discuss · Round 2"),
            _phase(20, 1, "Discuss · Round 3"),
        ),
        day_vote=_phase(20, 1, "Cast your vote"),
        mafia_discussion=_phase(15, 0, "Mafia discussion"),
        night_actions=_phase(15, 0, "Choose your night action"),
    ),
    CASUAL_STANDARD_CADENCE_ID: CadenceProfile(
        cadence_id=CASUAL_STANDARD_CADENCE_ID,
        mafia_intro=_phase(30, 0, "Meet your faction"),
        discussion_rounds=(
            _phase(75, 3, "Discuss · Round 1"),
            _phase(60, 3, "Discuss · Round 2"),
            _phase(45, 3, "Discuss · Round 3"),
        ),
        day_vote=_phase(45, 3, "Cast your vote"),
        mafia_discussion=_phase(30, 0, "Mafia discussion"),
        night_actions=_phase(30, 0, "Choose your night action"),
    ),
    CASUAL_RELAXED_CADENCE_ID: CadenceProfile(
        cadence_id=CASUAL_RELAXED_CADENCE_ID,
        mafia_intro=_phase(60, 0, "Meet your faction"),
        discussion_rounds=(
            _phase(120, 5, "Discuss · Round 1"),
            _phase(90, 5, "Discuss · Round 2"),
            _phase(75, 5, "Discuss · Round 3"),
        ),
        day_vote=_phase(75, 5, "Cast your vote"),
        mafia_discussion=_phase(60, 0, "Mafia discussion"),
        night_actions=_phase(60, 0, "Choose your night action"),
    ),
}


def cadence_profile(cadence_id: str) -> CadenceProfile:
    """Return a supported immutable cadence profile by id."""
    try:
        return _PROFILES[cadence_id]
    except KeyError as exc:
        raise ValueError(f"unknown_cadence:{cadence_id}") from exc


__all__ = [
    "BUILTIN_CADENCE_ID",
    "CASUAL_FAST_CADENCE_ID",
    "CASUAL_RELAXED_CADENCE_ID",
    "CASUAL_STANDARD_CADENCE_ID",
    "PLAYER_SELECTABLE_CADENCE_IDS",
    "SUPPORTED_CADENCE_IDS",
    "CadenceProfile",
    "PhaseCadence",
    "cadence_profile",
]
