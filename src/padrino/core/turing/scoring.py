"""Pure spot-the-AI (imitation-game) scorer (US-144).

After a human game terminates, each human submits one guess assigning HUMAN/AI to
every OTHER seat; this module computes that guesser's personal detection accuracy.

It is pure: no clock, no RNG, no IO, no DB. The impure shell resolves the seat
truth (who FINISHED the game as human vs AI - a taken-over seat counts as AI,
matching the canonical reveal) and the submitted guess, then hands them here.

There is NO competitive leaderboard in v1 (decision 6): the scorer only ever
computes one guesser's own personal stat, never a cross-player ranking.

Accuracy is emitted as a decimal *string*, never a float, so the value can flow
through the core's canonical-JSON discipline (hard rule 4: no floats in core).
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction

from pydantic import BaseModel, ConfigDict

#: The two legal guess labels a guesser may assign to another seat.
GUESS_HUMAN = "HUMAN"
GUESS_AI = "AI"
_LEGAL_LABELS = frozenset({GUESS_HUMAN, GUESS_AI})


class GuessScore(BaseModel):
    """One guesser's personal spot-the-AI result (no leaderboard)."""

    model_config = ConfigDict(frozen=True)

    guesser_public_id: str
    total: int
    correct: int
    #: ``correct / total`` as a decimal string (``"0"`` when ``total == 0``); the
    #: core never emits floats, so accuracy crosses the boundary as a string.
    accuracy: str


def _truth_label(is_human: bool) -> str:
    return GUESS_HUMAN if is_human else GUESS_AI


def _accuracy_string(*, correct: int, total: int) -> str:
    """Render ``correct / total`` exactly, never through binary float repr.

    A whole-number ratio (``0/2``, ``3/3``) renders without a decimal point
    (``"0"`` / ``"1"``); finite decimals render as decimals, and repeating
    fractions render as reduced ratios.
    """
    if total == 0:
        return "0"
    fraction = Fraction(correct, total)
    if fraction.denominator == 1:
        return str(fraction.numerator)

    denominator = fraction.denominator
    twos = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    fives = 0
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    if denominator != 1:
        return f"{fraction.numerator}/{fraction.denominator}"

    scale = max(twos, fives)
    scaled = fraction.numerator * (2 ** (scale - twos)) * (5 ** (scale - fives))
    digits = str(scaled).rjust(scale + 1, "0")
    whole = digits[:-scale]
    decimal = digits[-scale:].rstrip("0")
    return whole if not decimal else f"{whole}.{decimal}"


def score_guess(
    *,
    guesser_public_id: str,
    guess: Mapping[str, str],
    truth: Mapping[str, bool],
) -> GuessScore:
    """Score one guesser's HUMAN/AI guess against the seat truth (pure).

    ``truth`` maps every seat's ``public_player_id`` to whether a human FINISHED
    the game in that seat (a silently taken-over seat is ``False`` - it counts as
    AI, matching the canonical reveal). ``guess`` maps every OTHER seat to a label
    in :data:`GUESS_HUMAN` / :data:`GUESS_AI`.

    The guesser's own seat is excluded even if present in ``guess`` (a player does
    not guess their own identity). Raises :class:`ValueError` for an unknown seat,
    an invalid label, or an incomplete guess (a non-guesser seat left unguessed).
    """
    other_seats = {seat for seat in truth if seat != guesser_public_id}

    for seat, label in guess.items():
        if label not in _LEGAL_LABELS:
            raise ValueError(f"invalid guess label: {label!r}")
        if seat != guesser_public_id and seat not in truth:
            raise ValueError(f"unknown seat in guess: {seat!r}")

    guessed_seats = {seat for seat in guess if seat != guesser_public_id}
    missing = other_seats - guessed_seats
    if missing:
        raise ValueError(f"incomplete guess: missing {sorted(missing)!r}")

    total = len(other_seats)
    correct = sum(1 for seat in other_seats if guess[seat] == _truth_label(truth[seat]))
    accuracy = _accuracy_string(correct=correct, total=total)
    return GuessScore(
        guesser_public_id=guesser_public_id,
        total=total,
        correct=correct,
        accuracy=accuracy,
    )


__all__ = [
    "GUESS_AI",
    "GUESS_HUMAN",
    "GuessScore",
    "score_guess",
]
