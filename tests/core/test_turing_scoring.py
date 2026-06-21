"""Pure spot-the-AI scorer (US-144).

The scorer takes one guesser's HUMAN/AI guess for every OTHER seat plus the seat
truth and returns the guesser's personal detection accuracy. It is pure: no
clock, no RNG, no IO. There is NO leaderboard - only the guesser's own stat.
"""

from __future__ import annotations

import pytest

from padrino.core.turing import GUESS_AI, GUESS_HUMAN, GuessScore, score_guess


def test_all_correct_is_perfect_accuracy() -> None:
    truth = {"P02": True, "P03": False, "P04": False}  # is_human
    guess = {"P02": GUESS_HUMAN, "P03": GUESS_AI, "P04": GUESS_AI}
    result = score_guess(guesser_public_id="P01", guess=guess, truth=truth)
    assert result == GuessScore(
        guesser_public_id="P01",
        total=3,
        correct=3,
        accuracy="1",
    )


def test_all_wrong_is_zero_accuracy() -> None:
    truth = {"P02": True, "P03": False}
    guess = {"P02": GUESS_AI, "P03": GUESS_HUMAN}
    result = score_guess(guesser_public_id="P01", guess=guess, truth=truth)
    assert result.correct == 0
    assert result.total == 2
    assert result.accuracy == "0"


def test_repeating_partial_accuracy_is_an_exact_ratio_string() -> None:
    # 2 of 3 correct is repeating decimal, so render the exact reduced ratio.
    truth = {"P02": True, "P03": False, "P04": True}
    guess = {"P02": GUESS_HUMAN, "P03": GUESS_AI, "P04": GUESS_AI}
    result = score_guess(guesser_public_id="P01", guess=guess, truth=truth)
    assert result.correct == 2
    assert result.total == 3
    assert isinstance(result.accuracy, str)
    assert result.accuracy == "2/3"


def test_terminating_partial_accuracy_is_an_exact_decimal_string() -> None:
    truth = {"P02": True, "P03": False, "P04": False, "P05": False, "P06": False}
    guess = {
        "P02": GUESS_HUMAN,
        "P03": GUESS_AI,
        "P04": GUESS_AI,
        "P05": GUESS_AI,
        "P06": GUESS_HUMAN,
    }
    result = score_guess(guesser_public_id="P01", guess=guess, truth=truth)
    assert result.correct == 4
    assert result.total == 5
    assert result.accuracy == "0.8"


def test_guesser_seat_excluded_even_if_present_in_guess() -> None:
    # A guess that includes the guesser's own seat ignores it: a player does not
    # guess their own identity (they know it).
    truth = {"P01": False, "P02": True}
    guess = {"P01": GUESS_HUMAN, "P02": GUESS_HUMAN}
    result = score_guess(guesser_public_id="P01", guess=guess, truth=truth)
    assert result.total == 1
    assert result.correct == 1


def test_guess_for_unknown_seat_is_rejected() -> None:
    truth = {"P02": True}
    guess = {"P02": GUESS_HUMAN, "P99": GUESS_AI}
    with pytest.raises(ValueError, match="unknown seat"):
        score_guess(guesser_public_id="P01", guess=guess, truth=truth)


def test_invalid_label_is_rejected() -> None:
    truth = {"P02": True}
    guess = {"P02": "MAYBE"}
    with pytest.raises(ValueError, match="invalid guess label"):
        score_guess(guesser_public_id="P01", guess=guess, truth=truth)


def test_must_guess_every_other_seat() -> None:
    # A guess that omits a non-guesser seat is incomplete and rejected.
    truth = {"P02": True, "P03": False}
    guess = {"P02": GUESS_HUMAN}
    with pytest.raises(ValueError, match="incomplete guess"):
        score_guess(guesser_public_id="P01", guess=guess, truth=truth)


def test_empty_other_seats_yields_zero_total() -> None:
    # A degenerate game with only the guesser seat: nothing to guess.
    truth = {"P01": False}
    result = score_guess(guesser_public_id="P01", guess={}, truth=truth)
    assert result.total == 0
    assert result.correct == 0
    assert result.accuracy == "0"
