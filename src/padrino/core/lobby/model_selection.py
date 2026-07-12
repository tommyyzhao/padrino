"""Pure deterministic catalog-model auto-fill policy for casual lobbies.

The API/database shell resolves catalog rows into these deliberately small facts.
This module knows neither catalog ids nor providers; it only applies a frozen
funding/price eligibility policy and assigns canonical slugs from a seeded draw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from padrino.core.engine.rng import SeededRng


class NotEnoughEligibleModelsError(ValueError):
    """Raised when auto-fill cannot satisfy every requested AI seat."""

    def __init__(self, *, empty_seats: int, eligible_models: int) -> None:
        self.empty_seats = empty_seats
        self.eligible_models = eligible_models
        super().__init__(
            f"eligible model pool of {eligible_models} model(s) is smaller than "
            f"{empty_seats} empty seat(s)"
        )


@dataclass(frozen=True, slots=True)
class ModelAutoFillCandidate:
    """One catalog-derived candidate stripped to policy-relevant facts."""

    canonical_slug: str
    platform_eligible: bool
    byok_eligible: bool
    total_price_micros_per_million: int | None

    def __post_init__(self) -> None:
        if not self.canonical_slug:
            raise ValueError("canonical_slug_required")
        if (
            self.total_price_micros_per_million is not None
            and self.total_price_micros_per_million < 0
        ):
            raise ValueError("total_price_micros_per_million_non_negative")


@dataclass(frozen=True, slots=True)
class ModelAutoFillPolicy:
    """Injected policy deciding the eligible catalog population."""

    funding_source: Literal["platform", "byok"]
    max_total_price_micros_per_million: int | None = None

    def __post_init__(self) -> None:
        if (
            self.max_total_price_micros_per_million is not None
            and self.max_total_price_micros_per_million < 0
        ):
            raise ValueError("max_total_price_micros_per_million_non_negative")


def _eligible(candidate: ModelAutoFillCandidate, policy: ModelAutoFillPolicy) -> bool:
    if policy.funding_source == "byok":
        return candidate.byok_eligible
    if not candidate.platform_eligible:
        return False
    if policy.max_total_price_micros_per_million is None:
        return True
    return (
        candidate.total_price_micros_per_million is not None
        and candidate.total_price_micros_per_million <= policy.max_total_price_micros_per_million
    )


def autofill_model_slugs(
    *,
    lobby_seed: str,
    empty_seat_indices: tuple[int, ...] | list[int],
    reserved_model_slugs: dict[int, str],
    candidates: tuple[ModelAutoFillCandidate, ...] | list[ModelAutoFillCandidate],
    policy: ModelAutoFillPolicy,
) -> dict[int, str]:
    """Select distinct eligible canonical slugs for empty AI seats.

    Candidate ordering, repeated indexes, and duplicate catalog facts cannot
    affect the output.  Explicit duplicate selections are intentionally handled
    outside this function; auto-fill itself keeps each drawn slug distinct.
    """
    seat_indices = sorted(set(empty_seat_indices))
    reserved = set(reserved_model_slugs.values())
    candidates_by_slug = {candidate.canonical_slug: candidate for candidate in candidates}
    eligible = sorted(
        slug
        for slug, candidate in candidates_by_slug.items()
        if slug not in reserved and _eligible(candidate, policy)
    )
    if len(eligible) < len(seat_indices):
        raise NotEnoughEligibleModelsError(
            empty_seats=len(seat_indices), eligible_models=len(eligible)
        )

    shuffled = SeededRng(f"catalog-autofill:{lobby_seed}").shuffle(eligible)
    return dict(zip(seat_indices, shuffled, strict=False))


__all__ = [
    "ModelAutoFillCandidate",
    "ModelAutoFillPolicy",
    "NotEnoughEligibleModelsError",
    "autofill_model_slugs",
]
