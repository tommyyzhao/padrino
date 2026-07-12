"""Pure OpenRouter compatibility and qualification policy coverage (US-345)."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from padrino.core.model_qualification import (
    CompatibilityFacts,
    ModelQualificationPolicy,
    ProbeEvidenceFacts,
    ProviderAvailabilityFacts,
    QualificationReason,
    QualificationTier,
    evaluate_model_qualification,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "padrino" / "core" / "model_qualification.py"
)


def _policy() -> ModelQualificationPolicy:
    return ModelQualificationPolicy(
        current_time_bucket=100,
        max_probe_age_buckets=10,
        playable_max_tokens=1_000,
        playable_max_latency_ms=5_000,
        ranked_max_latency_ms=1_000,
        ranked_max_price_micros_per_million=2_000_000,
        ranked_min_successful_samples=3,
    )


def _facts() -> CompatibilityFacts:
    return CompatibilityFacts(
        is_text_model=True,
        explicitly_disabled=False,
        explicitly_deprecated=False,
        expires_at_bucket=101,
        advertised_strict_schema=True,
        prompt_price_micros_per_million=100_000,
        completion_price_micros_per_million=200_000,
        provider_availability=ProviderAvailabilityFacts(
            pinned=True,
            available=True,
            fingerprint="provider-v1",
        ),
    )


def _probe(
    *,
    probe_id: str,
    observed_at_bucket: int = 100,
    strict_schema_observed: bool = True,
    tokens_used: int = 800,
    latency_ms: int = 900,
    schema_coercion_succeeded: bool = True,
    drift_resolved: bool | None = True,
    provider_fingerprint: str | None = "provider-v1",
) -> ProbeEvidenceFacts:
    return ProbeEvidenceFacts(
        probe_id=probe_id,
        observed_at_bucket=observed_at_bucket,
        schema_coercion_succeeded=schema_coercion_succeeded,
        strict_schema_observed=strict_schema_observed,
        tokens_used=tokens_used,
        latency_ms=latency_ms,
        drift_resolved=drift_resolved,
        provider_fingerprint=provider_fingerprint,
    )


def test_text_models_are_casually_visible_but_unverified_without_probe_evidence() -> None:
    decision = evaluate_model_qualification(_facts(), (), policy=_policy())

    assert decision.tier is QualificationTier.UNVERIFIED
    assert decision.casual_visible is True
    assert decision.reasons == (QualificationReason.PROBE_EVIDENCE_MISSING,)


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (replace(_facts(), explicitly_disabled=True), QualificationReason.MODEL_DISABLED),
        (replace(_facts(), explicitly_deprecated=True), QualificationReason.MODEL_DEPRECATED),
        (replace(_facts(), expires_at_bucket=100), QualificationReason.MODEL_EXPIRED),
        (replace(_facts(), is_text_model=False), QualificationReason.NOT_TEXT_MODEL),
    ],
)
def test_disabled_deprecated_or_non_text_models_are_not_casually_visible(
    facts: CompatibilityFacts,
    reason: QualificationReason,
) -> None:
    decision = evaluate_model_qualification(facts, (), policy=_policy())

    assert decision.tier is QualificationTier.UNVERIFIED
    assert decision.casual_visible is False
    assert decision.reasons[0] is reason


def test_successful_bounded_schema_coercion_probe_makes_a_model_playable() -> None:
    decision = evaluate_model_qualification(
        replace(_facts(), advertised_strict_schema=False),
        [_probe(probe_id="one")],
        policy=_policy(),
    )

    assert decision.tier is QualificationTier.PLAYABLE
    assert decision.casual_visible is True
    assert decision.reasons == (
        QualificationReason.STRICT_SCHEMA_NOT_ADVERTISED,
        QualificationReason.INSUFFICIENT_SUCCESSFUL_SAMPLES,
    )


@pytest.mark.parametrize(
    ("facts", "probe", "reason", "tier"),
    [
        (
            replace(_facts(), advertised_strict_schema=False),
            _probe(probe_id="one"),
            QualificationReason.STRICT_SCHEMA_NOT_ADVERTISED,
            QualificationTier.PLAYABLE,
        ),
        (
            _facts(),
            _probe(probe_id="one", strict_schema_observed=False),
            QualificationReason.STRICT_SCHEMA_NOT_OBSERVED,
            QualificationTier.PLAYABLE,
        ),
        (
            _facts(),
            _probe(probe_id="one", tokens_used=1_001),
            QualificationReason.PROBE_TOKEN_LIMIT_EXCEEDED,
            QualificationTier.UNVERIFIED,
        ),
        (
            _facts(),
            _probe(probe_id="one", latency_ms=5_001),
            QualificationReason.PROBE_LATENCY_LIMIT_EXCEEDED,
            QualificationTier.UNVERIFIED,
        ),
        (
            _facts(),
            _probe(probe_id="one", observed_at_bucket=89),
            QualificationReason.PROBE_EXPIRED,
            QualificationTier.UNVERIFIED,
        ),
        (
            _facts(),
            _probe(probe_id="one", schema_coercion_succeeded=False),
            QualificationReason.PROBE_SCHEMA_COERCION_FAILED,
            QualificationTier.UNVERIFIED,
        ),
    ],
)
def test_playable_requires_a_recent_bounded_successful_schema_coercion_probe(
    facts: CompatibilityFacts,
    probe: ProbeEvidenceFacts,
    reason: QualificationReason,
    tier: QualificationTier,
) -> None:
    decision = evaluate_model_qualification(facts, [probe], policy=_policy())

    assert decision.tier is tier
    assert reason in decision.reasons


def test_verified_structured_requires_both_advertised_and_observed_strict_schema() -> None:
    decision = evaluate_model_qualification(
        _facts(),
        [_probe(probe_id="one"), _probe(probe_id="two"), _probe(probe_id="three")],
        policy=_policy(),
    )

    assert decision.tier is QualificationTier.RANKED_QUALIFIED
    assert decision.reasons == ()


def test_ranked_requires_pinned_available_provider_price_latency_sample_and_resolved_drift() -> (
    None
):
    probes = [_probe(probe_id=str(index)) for index in range(1, 4)]
    baseline = evaluate_model_qualification(_facts(), probes, policy=_policy())
    assert baseline.tier is QualificationTier.RANKED_QUALIFIED

    cases = [
        (
            replace(
                _facts(),
                provider_availability=ProviderAvailabilityFacts(
                    pinned=False, available=True, fingerprint="provider-v1"
                ),
            ),
            probes,
            QualificationReason.PROVIDER_AVAILABILITY_UNPINNED,
        ),
        (
            replace(
                _facts(),
                provider_availability=ProviderAvailabilityFacts(
                    pinned=True, available=False, fingerprint="provider-v1"
                ),
            ),
            probes,
            QualificationReason.PROVIDER_UNAVAILABLE,
        ),
        (
            replace(_facts(), prompt_price_micros_per_million=None),
            probes,
            QualificationReason.RANKED_PRICE_UNKNOWN,
        ),
        (
            replace(_facts(), completion_price_micros_per_million=2_000_001),
            probes,
            QualificationReason.RANKED_PRICE_LIMIT_EXCEEDED,
        ),
        (
            _facts(),
            [
                _probe(probe_id="one"),
                _probe(probe_id="two"),
                _probe(probe_id="three", latency_ms=1_001),
            ],
            QualificationReason.RANKED_LATENCY_LIMIT_EXCEEDED,
        ),
        (
            _facts(),
            probes[:2],
            QualificationReason.INSUFFICIENT_SUCCESSFUL_SAMPLES,
        ),
        (
            _facts(),
            [
                _probe(probe_id="one"),
                _probe(probe_id="two"),
                _probe(probe_id="three", drift_resolved=False),
            ],
            QualificationReason.PROBE_DRIFT_UNRESOLVED,
        ),
    ]

    for facts, case_probes, reason in cases:
        decision = evaluate_model_qualification(facts, case_probes, policy=_policy())
        assert decision.tier is QualificationTier.VERIFIED_STRUCTURED
        assert reason in decision.reasons


def test_free_models_are_not_promoted_from_popularity_and_can_qualify_on_facts() -> None:
    free = replace(
        _facts(),
        prompt_price_micros_per_million=0,
        completion_price_micros_per_million=0,
        popularity_rank=1,
    )
    no_probe = evaluate_model_qualification(free, (), policy=_policy())
    qualified = evaluate_model_qualification(
        free,
        [_probe(probe_id="one"), _probe(probe_id="two"), _probe(probe_id="three")],
        policy=_policy(),
    )

    assert no_probe.tier is QualificationTier.UNVERIFIED
    assert qualified.tier is QualificationTier.RANKED_QUALIFIED


def test_provider_fingerprint_change_is_unresolved_probe_drift() -> None:
    decision = evaluate_model_qualification(
        _facts(),
        [
            _probe(probe_id="one"),
            _probe(probe_id="two"),
            _probe(probe_id="three", provider_fingerprint="provider-v2"),
        ],
        policy=_policy(),
    )

    assert decision.tier is QualificationTier.VERIFIED_STRUCTURED
    assert QualificationReason.PROBE_DRIFT_UNRESOLVED in decision.reasons


def test_reason_order_and_time_bucket_are_deterministic_regardless_of_input_order() -> None:
    facts = replace(
        _facts(),
        advertised_strict_schema=False,
        prompt_price_micros_per_million=None,
        provider_availability=ProviderAvailabilityFacts(pinned=False, available=False),
    )
    probes = [
        _probe(probe_id="later", observed_at_bucket=99, latency_ms=1_001, drift_resolved=False),
        _probe(probe_id="earlier", observed_at_bucket=98, strict_schema_observed=False),
        _probe(probe_id="bad", observed_at_bucket=100, schema_coercion_succeeded=False),
    ]

    first = evaluate_model_qualification(facts, probes, policy=_policy())
    second = evaluate_model_qualification(facts, reversed(probes), policy=_policy())

    assert first == second
    assert first.reasons == (
        QualificationReason.PROBE_SCHEMA_COERCION_FAILED,
        QualificationReason.STRICT_SCHEMA_NOT_ADVERTISED,
        QualificationReason.PROVIDER_AVAILABILITY_UNPINNED,
        QualificationReason.PROVIDER_UNAVAILABLE,
        QualificationReason.RANKED_PRICE_UNKNOWN,
        QualificationReason.INSUFFICIENT_SUCCESSFUL_SAMPLES,
        QualificationReason.PROBE_DRIFT_UNRESOLVED,
    )


def test_qualification_policy_module_has_no_impure_imports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    offenders: list[tuple[int, str]] = []
    forbidden_exact = {"random", "datetime", "time"}
    forbidden_prefixes = ("padrino.db", "padrino.settings", "padrino.openrouter")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""]
        else:
            continue
        for module in imported:
            if module in forbidden_exact or module.startswith(forbidden_prefixes):
                offenders.append((node.lineno, module))

    assert not offenders
