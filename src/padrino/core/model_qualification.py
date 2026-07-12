"""Pure compatibility and qualification policy for discovered text models.

Database and OpenRouter shells normalize source catalog rows and probe records
into the facts below.  This module then produces the single authoritative tier
decision without consulting a clock, provider, or persistent state.  Its
``current_time_bucket`` input makes evidence expiry reproducible in tests and
in archived admission decisions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class QualificationTier(StrEnum):
    """Compatibility tiers shown to casual players and future ranked policy."""

    UNVERIFIED = "unverified"
    PLAYABLE = "playable"
    VERIFIED_STRUCTURED = "verified_structured"
    RANKED_QUALIFIED = "ranked_qualified"


class QualificationReason(StrEnum):
    """Ordered, machine-readable reasons a model did not reach a higher tier."""

    NOT_TEXT_MODEL = "not_text_model"
    MODEL_DISABLED = "model_disabled"
    MODEL_DEPRECATED = "model_deprecated"
    MODEL_EXPIRED = "model_expired"
    PROBE_EVIDENCE_MISSING = "probe_evidence_missing"
    PROBE_EXPIRED = "probe_expired"
    PROBE_SCHEMA_COERCION_FAILED = "probe_schema_coercion_failed"
    PROBE_TOKEN_LIMIT_EXCEEDED = "probe_token_limit_exceeded"
    PROBE_LATENCY_LIMIT_EXCEEDED = "probe_latency_limit_exceeded"
    STRICT_SCHEMA_NOT_ADVERTISED = "strict_schema_not_advertised"
    STRICT_SCHEMA_NOT_OBSERVED = "strict_schema_not_observed"
    PROVIDER_AVAILABILITY_UNPINNED = "provider_availability_unpinned"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RANKED_PRICE_UNKNOWN = "ranked_price_unknown"
    RANKED_PRICE_LIMIT_EXCEEDED = "ranked_price_limit_exceeded"
    INSUFFICIENT_SUCCESSFUL_SAMPLES = "insufficient_successful_samples"
    RANKED_LATENCY_LIMIT_EXCEEDED = "ranked_latency_limit_exceeded"
    PROBE_DRIFT_UNRESOLVED = "probe_drift_unresolved"


@dataclass(frozen=True, slots=True)
class ProviderAvailabilityFacts:
    """Resolved provider-routing facts needed only for ranked qualification."""

    pinned: bool | None = None
    available: bool | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CompatibilityFacts:
    """One normalized catalog model's non-probe compatibility facts.

    ``popularity_rank`` intentionally has no effect on the policy.  It remains
    an explicit ignored input to make it impossible for a caller to mistake
    popularity for compatibility evidence.
    """

    is_text_model: bool | None = None
    explicitly_disabled: bool | None = None
    explicitly_deprecated: bool | None = None
    expires_at_bucket: int | None = None
    advertised_strict_schema: bool | None = None
    prompt_price_micros_per_million: int | None = None
    completion_price_micros_per_million: int | None = None
    provider_availability: ProviderAvailabilityFacts | None = None
    popularity_rank: int | None = None


@dataclass(frozen=True, slots=True)
class ProbeEvidenceFacts:
    """One normalized, append-only compatibility probe observation."""

    probe_id: str
    observed_at_bucket: int
    schema_coercion_succeeded: bool | None = None
    strict_schema_observed: bool | None = None
    tokens_used: int | None = None
    latency_ms: int | None = None
    drift_resolved: bool | None = None
    provider_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ModelQualificationPolicy:
    """Operator-owned bounds supplied to the deterministic qualification policy."""

    current_time_bucket: int
    max_probe_age_buckets: int
    playable_max_tokens: int
    playable_max_latency_ms: int
    ranked_max_latency_ms: int
    ranked_max_price_micros_per_million: int
    ranked_min_successful_samples: int


@dataclass(frozen=True, slots=True)
class ModelQualificationDecision:
    """The derived tier, player-facing visibility, and stable denial reasons."""

    tier: QualificationTier
    casual_visible: bool
    reasons: tuple[QualificationReason, ...]


def evaluate_model_qualification(
    facts: CompatibilityFacts | None,
    evidence: Iterable[ProbeEvidenceFacts],
    *,
    policy: ModelQualificationPolicy,
) -> ModelQualificationDecision:
    """Derive a compatibility tier from catalog facts and immutable probes.

    A text model remains casually visible at ``UNVERIFIED`` until positive probe
    evidence establishes a higher tier.  Explicitly disabled, deprecated, or
    non-text catalog rows are never casually visible.  Higher tiers all fail
    closed when their required facts are absent.
    """

    if facts is None:
        return ModelQualificationDecision(
            tier=QualificationTier.UNVERIFIED,
            casual_visible=False,
            reasons=(QualificationReason.NOT_TEXT_MODEL,),
        )

    catalog_reasons = _catalog_reasons(facts, policy=policy)
    if catalog_reasons:
        return ModelQualificationDecision(
            tier=QualificationTier.UNVERIFIED,
            casual_visible=False,
            reasons=catalog_reasons,
        )

    probes = tuple(sorted(evidence, key=_probe_sort_key))
    playable_reasons, latest, successful = _playable_probe_facts(probes, policy=policy)
    reasons = list(playable_reasons)

    if probes:
        if facts.advertised_strict_schema is not True:
            reasons.append(QualificationReason.STRICT_SCHEMA_NOT_ADVERTISED)
        elif latest is not None and latest.strict_schema_observed is not True:
            reasons.append(QualificationReason.STRICT_SCHEMA_NOT_OBSERVED)

        _append_ranked_static_reasons(reasons, facts, policy=policy)
        if len(successful) < policy.ranked_min_successful_samples:
            reasons.append(QualificationReason.INSUFFICIENT_SUCCESSFUL_SAMPLES)
        elif any(
            probe.latency_ms is None or probe.latency_ms > policy.ranked_max_latency_ms
            for probe in successful
        ):
            reasons.append(QualificationReason.RANKED_LATENCY_LIMIT_EXCEEDED)
        if _has_unresolved_drift(successful, facts):
            reasons.append(QualificationReason.PROBE_DRIFT_UNRESOLVED)

    playable = not playable_reasons
    verified_structured = (
        playable
        and facts.advertised_strict_schema is True
        and latest is not None
        and latest.strict_schema_observed is True
    )
    ranked_reasons = _ranked_reasons(reasons)
    tier = QualificationTier.UNVERIFIED
    if playable:
        tier = QualificationTier.PLAYABLE
    if verified_structured:
        tier = QualificationTier.VERIFIED_STRUCTURED
    if verified_structured and not ranked_reasons:
        tier = QualificationTier.RANKED_QUALIFIED

    return ModelQualificationDecision(
        tier=tier,
        casual_visible=True,
        reasons=tuple(_ordered_unique(reasons)),
    )


def _catalog_reasons(
    facts: CompatibilityFacts,
    *,
    policy: ModelQualificationPolicy,
) -> tuple[QualificationReason, ...]:
    reasons: list[QualificationReason] = []
    if facts.is_text_model is not True:
        reasons.append(QualificationReason.NOT_TEXT_MODEL)
    if facts.explicitly_disabled is True:
        reasons.append(QualificationReason.MODEL_DISABLED)
    if facts.explicitly_deprecated is True:
        reasons.append(QualificationReason.MODEL_DEPRECATED)
    if (
        facts.expires_at_bucket is not None
        and facts.expires_at_bucket <= policy.current_time_bucket
    ):
        reasons.append(QualificationReason.MODEL_EXPIRED)
    return tuple(reasons)


def _playable_probe_facts(
    probes: tuple[ProbeEvidenceFacts, ...],
    *,
    policy: ModelQualificationPolicy,
) -> tuple[list[QualificationReason], ProbeEvidenceFacts | None, tuple[ProbeEvidenceFacts, ...]]:
    if not probes:
        return [QualificationReason.PROBE_EVIDENCE_MISSING], None, ()

    fresh = tuple(probe for probe in probes if _is_fresh(probe, policy=policy))
    if not fresh:
        return [QualificationReason.PROBE_EXPIRED], None, ()

    latest = fresh[-1]
    reasons: list[QualificationReason] = []
    if latest.schema_coercion_succeeded is not True:
        reasons.append(QualificationReason.PROBE_SCHEMA_COERCION_FAILED)
    if not _within_playable_token_bound(latest, policy=policy):
        reasons.append(QualificationReason.PROBE_TOKEN_LIMIT_EXCEEDED)
    if not _within_playable_latency_bound(latest, policy=policy):
        reasons.append(QualificationReason.PROBE_LATENCY_LIMIT_EXCEEDED)

    successful = tuple(
        probe
        for probe in fresh
        if probe.schema_coercion_succeeded is True
        and _within_playable_token_bound(probe, policy=policy)
        and _within_playable_latency_bound(probe, policy=policy)
    )
    return reasons, latest, successful


def _append_ranked_static_reasons(
    reasons: list[QualificationReason],
    facts: CompatibilityFacts,
    *,
    policy: ModelQualificationPolicy,
) -> None:
    provider = facts.provider_availability
    if (
        provider is None
        or provider.pinned is not True
        or not _has_fingerprint(provider.fingerprint)
    ):
        reasons.append(QualificationReason.PROVIDER_AVAILABILITY_UNPINNED)
    if provider is None or provider.available is not True:
        reasons.append(QualificationReason.PROVIDER_UNAVAILABLE)
    prices = (
        facts.prompt_price_micros_per_million,
        facts.completion_price_micros_per_million,
    )
    if any(not _non_negative_int(price) for price in prices):
        reasons.append(QualificationReason.RANKED_PRICE_UNKNOWN)
    elif any(
        isinstance(price, int) and price > policy.ranked_max_price_micros_per_million
        for price in prices
    ):
        reasons.append(QualificationReason.RANKED_PRICE_LIMIT_EXCEEDED)


def _has_unresolved_drift(
    successful: tuple[ProbeEvidenceFacts, ...],
    facts: CompatibilityFacts,
) -> bool:
    provider = facts.provider_availability
    current_fingerprint = provider.fingerprint if provider is not None else None
    return any(
        probe.drift_resolved is not True
        or not _has_fingerprint(probe.provider_fingerprint)
        or probe.provider_fingerprint != current_fingerprint
        for probe in successful
    )


def _ranked_reasons(reasons: list[QualificationReason]) -> tuple[QualificationReason, ...]:
    rank_blockers = {
        QualificationReason.PROVIDER_AVAILABILITY_UNPINNED,
        QualificationReason.PROVIDER_UNAVAILABLE,
        QualificationReason.RANKED_PRICE_UNKNOWN,
        QualificationReason.RANKED_PRICE_LIMIT_EXCEEDED,
        QualificationReason.INSUFFICIENT_SUCCESSFUL_SAMPLES,
        QualificationReason.RANKED_LATENCY_LIMIT_EXCEEDED,
        QualificationReason.PROBE_DRIFT_UNRESOLVED,
    }
    return tuple(reason for reason in reasons if reason in rank_blockers)


def _is_fresh(probe: ProbeEvidenceFacts, *, policy: ModelQualificationPolicy) -> bool:
    age = policy.current_time_bucket - probe.observed_at_bucket
    return 0 <= age <= policy.max_probe_age_buckets


def _within_playable_token_bound(
    probe: ProbeEvidenceFacts,
    *,
    policy: ModelQualificationPolicy,
) -> bool:
    tokens_used = probe.tokens_used
    return (
        _non_negative_int(tokens_used)
        and isinstance(tokens_used, int)
        and tokens_used <= policy.playable_max_tokens
    )


def _within_playable_latency_bound(
    probe: ProbeEvidenceFacts,
    *,
    policy: ModelQualificationPolicy,
) -> bool:
    latency_ms = probe.latency_ms
    return (
        _non_negative_int(latency_ms)
        and isinstance(latency_ms, int)
        and latency_ms <= policy.playable_max_latency_ms
    )


def _non_negative_int(value: int | None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _has_fingerprint(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _probe_sort_key(probe: ProbeEvidenceFacts) -> tuple[int, str]:
    return probe.observed_at_bucket, probe.probe_id


def _ordered_unique(
    reasons: list[QualificationReason],
) -> tuple[QualificationReason, ...]:
    order = {reason: index for index, reason in enumerate(QualificationReason)}
    return tuple(sorted(set(reasons), key=order.__getitem__))


__all__ = [
    "CompatibilityFacts",
    "ModelQualificationDecision",
    "ModelQualificationPolicy",
    "ProbeEvidenceFacts",
    "ProviderAvailabilityFacts",
    "QualificationReason",
    "QualificationTier",
    "evaluate_model_qualification",
]
