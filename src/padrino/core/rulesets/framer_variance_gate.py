"""Pure variance gate for admitting Framer into canonical rulesets."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

FRAMER_MIN_MIRROR_PAIRED_GAMES: Final[int] = 200
FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE: Final[Fraction] = Fraction(15, 100)

FramerVarianceGateReason = Literal[
    "enabled",
    "insufficient_mirror_pairs",
    "frame_rng_variance_budget_exceeded",
]


@dataclass(frozen=True)
class FramerVarianceGateResult:
    """Outcome of the pinned Framer canonical-admission gate."""

    enabled: bool
    mirror_paired_games: int
    frame_target_rng_variance_share: Fraction
    reason: FramerVarianceGateReason


def compute_frame_rng_variance_share(
    *,
    frame_target_rng_variance: Fraction,
    measured_skill_delta: Fraction,
) -> Fraction:
    """Return the share of measured signal attributable to frame-target RNG."""
    if frame_target_rng_variance < 0:
        raise ValueError("frame_target_rng_variance must be non-negative")
    if measured_skill_delta < 0:
        raise ValueError("measured_skill_delta must be non-negative")

    denominator = frame_target_rng_variance + measured_skill_delta
    if denominator == 0:
        return Fraction(1)
    return frame_target_rng_variance / denominator


def evaluate_framer_variance_gate(
    *,
    mirror_paired_games: int,
    frame_target_rng_variance: Fraction,
    measured_skill_delta: Fraction,
) -> FramerVarianceGateResult:
    """Evaluate whether Framer may be enabled in a canonical ruleset."""
    share = compute_frame_rng_variance_share(
        frame_target_rng_variance=frame_target_rng_variance,
        measured_skill_delta=measured_skill_delta,
    )
    if mirror_paired_games < FRAMER_MIN_MIRROR_PAIRED_GAMES:
        return FramerVarianceGateResult(
            enabled=False,
            mirror_paired_games=mirror_paired_games,
            frame_target_rng_variance_share=share,
            reason="insufficient_mirror_pairs",
        )
    if share > FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE:
        return FramerVarianceGateResult(
            enabled=False,
            mirror_paired_games=mirror_paired_games,
            frame_target_rng_variance_share=share,
            reason="frame_rng_variance_budget_exceeded",
        )
    return FramerVarianceGateResult(
        enabled=True,
        mirror_paired_games=mirror_paired_games,
        frame_target_rng_variance_share=share,
        reason="enabled",
    )


CURRENT_FRAMER_VARIANCE_GATE: Final[FramerVarianceGateResult] = evaluate_framer_variance_gate(
    mirror_paired_games=0,
    frame_target_rng_variance=Fraction(0),
    measured_skill_delta=Fraction(0),
)


__all__ = [
    "CURRENT_FRAMER_VARIANCE_GATE",
    "FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE",
    "FRAMER_MIN_MIRROR_PAIRED_GAMES",
    "FramerVarianceGateReason",
    "FramerVarianceGateResult",
    "compute_frame_rng_variance_share",
    "evaluate_framer_variance_gate",
]
