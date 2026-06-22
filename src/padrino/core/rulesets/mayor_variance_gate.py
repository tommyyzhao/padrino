"""Pure variance gate for admitting Mayor into canonical rulesets."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

MAYOR_MIN_MIRROR_PAIRED_GAMES: Final[int] = 200
MAYOR_MAX_WEIGHTED_VOTE_RNG_VARIANCE_SHARE: Final[Fraction] = Fraction(15, 100)

MayorVarianceGateReason = Literal[
    "enabled",
    "insufficient_mirror_pairs",
    "weighted_vote_rng_variance_budget_exceeded",
]


@dataclass(frozen=True)
class MayorVarianceGateResult:
    """Outcome of the pinned Mayor canonical-admission gate."""

    enabled: bool
    mirror_paired_games: int
    weighted_vote_rng_variance_share: Fraction
    reason: MayorVarianceGateReason


def compute_weighted_vote_rng_variance_share(
    *,
    weighted_vote_rng_variance: Fraction,
    measured_skill_delta: Fraction,
) -> Fraction:
    """Return the share of measured signal attributable to Mayor-vote RNG."""
    if weighted_vote_rng_variance < 0:
        raise ValueError("weighted_vote_rng_variance must be non-negative")
    if measured_skill_delta < 0:
        raise ValueError("measured_skill_delta must be non-negative")

    denominator = weighted_vote_rng_variance + measured_skill_delta
    if denominator == 0:
        return Fraction(1)
    return weighted_vote_rng_variance / denominator


def evaluate_mayor_variance_gate(
    *,
    mirror_paired_games: int,
    weighted_vote_rng_variance: Fraction,
    measured_skill_delta: Fraction,
) -> MayorVarianceGateResult:
    """Evaluate whether Mayor may be enabled in a canonical ruleset."""
    share = compute_weighted_vote_rng_variance_share(
        weighted_vote_rng_variance=weighted_vote_rng_variance,
        measured_skill_delta=measured_skill_delta,
    )
    if mirror_paired_games < MAYOR_MIN_MIRROR_PAIRED_GAMES:
        return MayorVarianceGateResult(
            enabled=False,
            mirror_paired_games=mirror_paired_games,
            weighted_vote_rng_variance_share=share,
            reason="insufficient_mirror_pairs",
        )
    if share > MAYOR_MAX_WEIGHTED_VOTE_RNG_VARIANCE_SHARE:
        return MayorVarianceGateResult(
            enabled=False,
            mirror_paired_games=mirror_paired_games,
            weighted_vote_rng_variance_share=share,
            reason="weighted_vote_rng_variance_budget_exceeded",
        )
    return MayorVarianceGateResult(
        enabled=True,
        mirror_paired_games=mirror_paired_games,
        weighted_vote_rng_variance_share=share,
        reason="enabled",
    )


CURRENT_MAYOR_VARIANCE_GATE: Final[MayorVarianceGateResult] = evaluate_mayor_variance_gate(
    mirror_paired_games=0,
    weighted_vote_rng_variance=Fraction(0),
    measured_skill_delta=Fraction(0),
)


__all__ = [
    "CURRENT_MAYOR_VARIANCE_GATE",
    "MAYOR_MAX_WEIGHTED_VOTE_RNG_VARIANCE_SHARE",
    "MAYOR_MIN_MIRROR_PAIRED_GAMES",
    "MayorVarianceGateReason",
    "MayorVarianceGateResult",
    "compute_weighted_vote_rng_variance_share",
    "evaluate_mayor_variance_gate",
]
