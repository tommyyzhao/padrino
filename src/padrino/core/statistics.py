"""Pure-core statistics helpers for gauntlet evaluation reports (US-077).

Closed-form Wilson score confidence interval for a binomial proportion and a
deterministic-bootstrap helper for paired-sample mean differences. Both stay
inside the pure-core firewall — no ``random``, no wall-clock, no I/O.

The Wilson interval is preferred over the normal-approximation (Wald)
interval because it stays inside ``[0, 1]`` and does not collapse to a
degenerate width when ``k == 0`` or ``k == n``. That property matters at the
sample sizes mini-gauntlets produce (``n`` in the single digits): the Wald
interval would silently report ``(0, 0)`` or ``(1, 1)`` for unanimous
outcomes, while Wilson yields a proper non-degenerate band.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from padrino.core.engine.rng import SeededRng

# Two-sided z critical value for 95% confidence. Hard-coded rather than
# pulled from ``scipy.stats`` to keep the pure-core firewall closed.
Z_95: float = 1.959963984540054


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """Two-sided confidence interval ``[lower, upper]`` with the point estimate."""

    point: float
    lower: float
    upper: float


def wilson_score_interval(
    successes: int,
    trials: int,
    *,
    z: float = Z_95,
) -> ConfidenceInterval:
    """Return the Wilson score confidence interval for a binomial proportion.

    ``successes`` must satisfy ``0 <= successes <= trials``. When ``trials``
    is zero the convention here is ``ConfidenceInterval(0.0, 0.0, 1.0)`` —
    the point estimate degenerates to zero but the interval spans the whole
    probability simplex so a downstream consumer can tell "no data" apart
    from "definitely zero".
    """
    if successes < 0 or trials < 0:
        raise ValueError("successes and trials must be non-negative")
    if successes > trials:
        raise ValueError("successes cannot exceed trials")
    if trials == 0:
        return ConfidenceInterval(point=0.0, lower=0.0, upper=1.0)

    n = float(trials)
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)) / denom
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    # Snap to exact 0 / 1 at the trivial boundaries so callers can pattern-match
    # "no failure observed" / "no success observed" without floating-point fuzz.
    if successes == 0:
        lower = 0.0
    if successes == trials:
        upper = 1.0
    return ConfidenceInterval(point=p_hat, lower=lower, upper=upper)


def bootstrap_mean_ci(
    samples: Sequence[float],
    rng: SeededRng,
    *,
    iterations: int = 1000,
    confidence: float = 0.95,
) -> ConfidenceInterval:
    """Return the percentile bootstrap CI for the mean of ``samples``.

    Used by the gauntlet evaluation report to estimate whether a rating
    delta (``after_mu - before_mu``) is plausibly non-zero given the small
    per-agent sample sizes a mini-gauntlet produces. Deterministic given a
    seeded RNG so test fixtures replay bit-for-bit.

    Empty ``samples`` yield a ``ConfidenceInterval(0, 0, 0)``.
    A single sample yields a zero-width interval centered on that value.
    """
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")

    n = len(samples)
    if n == 0:
        return ConfidenceInterval(point=0.0, lower=0.0, upper=0.0)
    if n == 1:
        only = float(samples[0])
        return ConfidenceInterval(point=only, lower=only, upper=only)

    sample_mean = sum(samples) / n
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _draw in range(n):
            total += samples[rng.randbelow(n)]
        means.append(total / n)
    means.sort()
    alpha = 1.0 - confidence
    lo_idx = max(0, math.floor(alpha / 2.0 * iterations))
    hi_idx = min(iterations - 1, math.ceil((1.0 - alpha / 2.0) * iterations) - 1)
    return ConfidenceInterval(
        point=sample_mean,
        lower=means[lo_idx],
        upper=means[hi_idx],
    )


__all__ = [
    "Z_95",
    "ConfidenceInterval",
    "bootstrap_mean_ci",
    "wilson_score_interval",
]
