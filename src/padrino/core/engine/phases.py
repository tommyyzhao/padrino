"""Phase-sequence finite state machine for mini7-style rulesets.

`next_phase` is a pure function from the current phase + ruleset constants to
the next phase. It never reads state — terminal transitions driven by win
conditions are the resolver's responsibility; this FSM only enforces the
turn-order skeleton up to `MAX_DAYS`.

Sequence (per `prd.md` §5.3):
    SETUP
      -> NIGHT_0_MAFIA_INTRO
      -> DAY_1_DISCUSSION_ROUND_1 .. ROUND_3
      -> DAY_1_VOTE
      -> NIGHT_1_MAFIA_DISCUSSION
      -> NIGHT_1_ACTIONS
      -> DAY_2_... (repeat through MAX_DAYS)
      -> TERMINAL
"""

from __future__ import annotations

from typing import Protocol

from padrino.core.engine.state import Phase
from padrino.core.enums import PhaseKind


class Ruleset(Protocol):
    """Structural ruleset interface required by the phase FSM."""

    MAX_DAYS: int
    DISCUSSION_ROUNDS_PER_DAY: int


def next_phase(current: Phase, ruleset: Ruleset) -> Phase:
    """Return the phase that follows `current` under `ruleset`'s schedule.

    Raises `ValueError` if `current` is already `TERMINAL` or if its fields are
    inconsistent with the schedule (e.g. an out-of-range discussion round).
    """
    kind = current.kind
    day = current.day
    rnd = current.round
    rounds_per_day = ruleset.DISCUSSION_ROUNDS_PER_DAY
    max_days = ruleset.MAX_DAYS

    if kind is PhaseKind.SETUP:
        return Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0)

    if kind is PhaseKind.NIGHT_0_MAFIA_INTRO:
        return Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1)

    if kind is PhaseKind.DAY_DISCUSSION:
        if rnd < 1 or rnd > rounds_per_day:
            raise ValueError(f"invalid discussion round {rnd} for day {day}")
        if rnd < rounds_per_day:
            return Phase(kind=PhaseKind.DAY_DISCUSSION, day=day, round=rnd + 1)
        return Phase(kind=PhaseKind.DAY_VOTE, day=day, round=0)

    if kind is PhaseKind.DAY_VOTE:
        return Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=day, round=0)

    if kind is PhaseKind.NIGHT_MAFIA_DISCUSSION:
        return Phase(kind=PhaseKind.NIGHT_ACTIONS, day=day, round=0)

    if kind is PhaseKind.NIGHT_ACTIONS:
        if day >= max_days:
            return Phase(kind=PhaseKind.TERMINAL, day=day, round=0)
        return Phase(kind=PhaseKind.DAY_DISCUSSION, day=day + 1, round=1)

    if kind is PhaseKind.TERMINAL:
        raise ValueError("no phase follows TERMINAL")

    raise ValueError(f"unhandled phase kind: {kind!r}")
