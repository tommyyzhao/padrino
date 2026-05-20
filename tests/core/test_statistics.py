"""Tests for :mod:`padrino.core.statistics` — Wilson CI + bootstrap helpers.

These guard the pure-core math the US-077 evaluation report depends on:
non-degenerate intervals at low N, exact hand-computed values at canonical
sample points, and deterministic bootstrap output for a seeded RNG.
"""

from __future__ import annotations

import math

import pytest

from padrino.core.engine.rng import SeededRng
from padrino.core.statistics import (
    Z_95,
    ConfidenceInterval,
    bootstrap_mean_ci,
    wilson_score_interval,
)


def test_wilson_zero_trials_returns_full_simplex() -> None:
    ci = wilson_score_interval(0, 0)
    assert ci == ConfidenceInterval(point=0.0, lower=0.0, upper=1.0)


def test_wilson_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError):
        wilson_score_interval(-1, 5)
    with pytest.raises(ValueError):
        wilson_score_interval(2, -1)


def test_wilson_rejects_successes_exceeding_trials() -> None:
    with pytest.raises(ValueError):
        wilson_score_interval(6, 5)


def test_wilson_does_not_collapse_at_low_n() -> None:
    """At n=3 the interval must remain non-degenerate for every k.

    This is the post-Wave-2 audit follow-up that motivated US-077: the
    leaderboard cannot claim a faction win-rate of "exactly 100%" off three
    games even when all three came up the same way.
    """
    for k in range(0, 4):
        ci = wilson_score_interval(k, 3)
        assert 0.0 <= ci.lower < ci.upper <= 1.0, f"degenerate CI at k={k}: {ci}"
        assert ci.upper - ci.lower > 0.05, f"CI too narrow at k={k}: {ci}"


def test_wilson_zero_successes_lower_is_zero() -> None:
    ci = wilson_score_interval(0, 10)
    assert ci.point == 0.0
    assert ci.lower == 0.0
    assert 0.0 < ci.upper < 1.0


def test_wilson_all_successes_upper_is_one() -> None:
    ci = wilson_score_interval(10, 10)
    assert ci.point == 1.0
    assert ci.upper == 1.0
    assert 0.0 < ci.lower < 1.0


def test_wilson_hand_computed_value_at_k5_n10() -> None:
    """k=5, n=10, z=1.959963984540054 -> roughly (0.5, 0.2366, 0.7634).

    Manual computation:
        z2 = 3.84146;  denom = 1 + z2/10 = 1.384146
        center = (0.5 + z2/20) / denom = 0.69207 / 1.38415 = 0.5
        margin = z * sqrt((0.25 + z2/40) / 10) / denom = 0.26336
        => (0.2366, 0.7634)
    """
    ci = wilson_score_interval(5, 10)
    assert math.isclose(ci.point, 0.5)
    assert math.isclose(ci.lower, 0.236593090512564, abs_tol=1e-6)
    assert math.isclose(ci.upper, 0.7634069094874361, abs_tol=1e-6)
    # Sanity: symmetry around 0.5 (since k=n/2 exactly).
    assert math.isclose(ci.point - ci.lower, ci.upper - ci.point, abs_tol=1e-9)


def test_z_95_constant_is_two_sided_for_alpha_05() -> None:
    """Sanity-check the hard-coded Z critical value matches scipy.stats.norm.ppf(0.975)."""
    assert math.isclose(Z_95, 1.959963984540054, abs_tol=1e-12)


def test_bootstrap_empty_sample_returns_zero_ci() -> None:
    rng = SeededRng("bootstrap-empty")
    ci = bootstrap_mean_ci([], rng)
    assert ci == ConfidenceInterval(point=0.0, lower=0.0, upper=0.0)


def test_bootstrap_single_sample_is_zero_width() -> None:
    rng = SeededRng("bootstrap-single")
    ci = bootstrap_mean_ci([3.5], rng)
    assert ci.point == ci.lower == ci.upper == 3.5


def test_bootstrap_is_deterministic_for_seeded_rng() -> None:
    samples = [0.1, 0.2, -0.05, 0.4, 0.3, -0.1, 0.25, 0.0, 0.15, 0.5]
    ci_a = bootstrap_mean_ci(samples, SeededRng("seed-A"), iterations=500)
    ci_b = bootstrap_mean_ci(samples, SeededRng("seed-A"), iterations=500)
    ci_c = bootstrap_mean_ci(samples, SeededRng("seed-B"), iterations=500)
    assert ci_a == ci_b
    # Different seeds produce different bootstrap distributions, but the
    # point estimate (sample mean) is identical.
    assert ci_a.point == ci_c.point
    assert ci_a != ci_c


def test_bootstrap_brackets_sample_mean() -> None:
    samples = [0.5, 0.5, 0.5, 1.0, 0.0, 0.8, 0.2, 0.5, 0.5, 0.5]
    ci = bootstrap_mean_ci(samples, SeededRng("bracket"), iterations=2000)
    assert ci.lower <= ci.point <= ci.upper
    assert math.isclose(ci.point, sum(samples) / len(samples))


def test_bootstrap_rejects_invalid_arguments() -> None:
    rng = SeededRng("argval")
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], rng, iterations=0)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], rng, confidence=0.0)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([1.0, 2.0], rng, confidence=1.0)
