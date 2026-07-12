"""US-348 deterministic catalog-model auto-fill policy coverage."""

from __future__ import annotations

import pytest

from padrino.core.lobby.model_selection import (
    ModelAutoFillCandidate,
    ModelAutoFillPolicy,
    NotEnoughEligibleModelsError,
    autofill_model_slugs,
)


def _candidate(
    slug: str,
    *,
    platform_eligible: bool = True,
    byok_eligible: bool = True,
    total_price_micros_per_million: int = 1_000,
) -> ModelAutoFillCandidate:
    return ModelAutoFillCandidate(
        canonical_slug=slug,
        platform_eligible=platform_eligible,
        byok_eligible=byok_eligible,
        total_price_micros_per_million=total_price_micros_per_million,
    )


def test_autofill_model_slugs_is_seeded_and_applies_platform_price_and_compatibility_policy() -> (
    None
):
    policy = ModelAutoFillPolicy(
        funding_source="platform",
        max_total_price_micros_per_million=2_000,
    )
    candidates = (
        _candidate("openai/eligible-a"),
        _candidate("openai/expensive", total_price_micros_per_million=2_001),
        _candidate("openai/byok-only", platform_eligible=False),
        _candidate("openai/eligible-b"),
    )

    first = autofill_model_slugs(
        lobby_seed="selection-seed",
        empty_seat_indices=(6, 4),
        reserved_model_slugs={1: "openai/reserved"},
        candidates=candidates,
        policy=policy,
    )
    second = autofill_model_slugs(
        lobby_seed="selection-seed",
        empty_seat_indices=(4, 6),
        reserved_model_slugs={1: "openai/reserved"},
        candidates=tuple(reversed(candidates)),
        policy=policy,
    )

    assert first == second
    assert set(first) == {4, 6}
    assert set(first.values()) == {"openai/eligible-a", "openai/eligible-b"}


def test_autofill_model_slugs_uses_byok_eligibility_without_platform_price_cap() -> None:
    result = autofill_model_slugs(
        lobby_seed="byok-seed",
        empty_seat_indices=(2,),
        reserved_model_slugs={},
        candidates=(
            _candidate("openai/platform-only", byok_eligible=False),
            _candidate(
                "openai/byok-expensive",
                platform_eligible=False,
                total_price_micros_per_million=99_999,
            ),
        ),
        policy=ModelAutoFillPolicy(funding_source="byok"),
    )

    assert result == {2: "openai/byok-expensive"}


def test_autofill_model_slugs_fails_closed_when_eligible_pool_is_too_small() -> None:
    with pytest.raises(NotEnoughEligibleModelsError, match="eligible model"):
        autofill_model_slugs(
            lobby_seed="insufficient",
            empty_seat_indices=(1, 2),
            reserved_model_slugs={},
            candidates=(_candidate("openai/only-one"),),
            policy=ModelAutoFillPolicy(funding_source="platform"),
        )
