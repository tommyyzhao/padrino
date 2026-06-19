"""Pure spot-the-AI (imitation-game) scoring for the human lane (US-144).

After a human game terminates, each human guesses HUMAN/AI for every OTHER seat
and sees their personal detection accuracy. The scoring is pure (no clock / RNG /
IO): the impure shell resolves the seat truth from the terminal seat rows and the
submitted guess, then hands them here. There is NO competitive leaderboard in v1
(decision 6) - the scorer only ever computes one guesser's personal stat.
"""

from __future__ import annotations

from padrino.core.turing.scoring import (
    GUESS_AI,
    GUESS_HUMAN,
    GuessScore,
    score_guess,
)

__all__ = [
    "GUESS_AI",
    "GUESS_HUMAN",
    "GuessScore",
    "score_guess",
]
