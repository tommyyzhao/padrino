"""Tests for the Framer canonical variance gate."""

from __future__ import annotations

from fractions import Fraction

from padrino.core.enums import Role
from padrino.core.rulesets import BUILTIN_RULESET_IDS, get_ruleset
from padrino.core.rulesets.framer_variance_gate import (
    CURRENT_FRAMER_VARIANCE_GATE,
    FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE,
    FRAMER_MIN_MIRROR_PAIRED_GAMES,
    compute_frame_rng_variance_share,
    evaluate_framer_variance_gate,
)


def test_gate_computes_frame_rng_share_against_skill_delta() -> None:
    assert compute_frame_rng_variance_share(
        frame_target_rng_variance=Fraction(3, 20),
        measured_skill_delta=Fraction(17, 20),
    ) == Fraction(3, 20)


def test_gate_disables_framer_when_sample_is_too_small() -> None:
    result = evaluate_framer_variance_gate(
        mirror_paired_games=FRAMER_MIN_MIRROR_PAIRED_GAMES - 1,
        frame_target_rng_variance=Fraction(0),
        measured_skill_delta=Fraction(1),
    )

    assert result.enabled is False
    assert result.reason == "insufficient_mirror_pairs"


def test_gate_disables_framer_when_rng_share_exceeds_budget() -> None:
    result = evaluate_framer_variance_gate(
        mirror_paired_games=FRAMER_MIN_MIRROR_PAIRED_GAMES,
        frame_target_rng_variance=Fraction(16, 100),
        measured_skill_delta=Fraction(84, 100),
    )

    assert result.enabled is False
    assert result.frame_target_rng_variance_share == Fraction(16, 100)
    assert result.reason == "frame_rng_variance_budget_exceeded"


def test_gate_enables_framer_at_pinned_variance_budget() -> None:
    result = evaluate_framer_variance_gate(
        mirror_paired_games=FRAMER_MIN_MIRROR_PAIRED_GAMES,
        frame_target_rng_variance=FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE,
        measured_skill_delta=Fraction(85, 100),
    )

    assert result.enabled is True
    assert result.frame_target_rng_variance_share == FRAMER_MAX_FRAME_RNG_VARIANCE_SHARE
    assert result.reason == "enabled"


def test_current_canonical_rulesets_keep_framer_disabled_until_gate_passes() -> None:
    assert CURRENT_FRAMER_VARIANCE_GATE.enabled is False
    assert CURRENT_FRAMER_VARIANCE_GATE.reason == "insufficient_mirror_pairs"

    for ruleset_id in BUILTIN_RULESET_IDS:
        ruleset = get_ruleset(ruleset_id)
        if ruleset.IS_CANONICAL:
            assert Role.FRAMER not in ruleset.ROLE_COUNTS
