"""Safe, append-only OpenRouter compatibility probes.

Probe runs live entirely in the impure management plane.  They use canonical
synthetic observations rather than a game state, so an operator can establish
compatibility evidence without touching in-flight games, game events, or
runtime snapshots.  Only compact hashes and allow-listed provider facts are
persisted; raw prompts, responses, errors, and credentials never leave the
transport boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.rate_limit_store import DatabaseRateLimitStore
from padrino.core.agents.contract import AgentResponse, parse_agent_response
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import CatalogSnapshot, ModelCatalogEntry, ModelProbeEvidence
from padrino.db.repositories.model_catalog_snapshots import (
    create_or_get_probe_evidence,
    get_probe_evidence_by_key,
)

DEFAULT_PROBE_SUITE_VERSION = "openrouter_compatibility_v1"
_PROBE_SYSTEM_PROMPT = (
    "Return only a Padrino AgentResponse JSON object. Mechanical intent belongs only "
    "in action; chat text never changes game mechanics."
)
_MAX_OUTPUT_TOKENS = 512
_MAX_PROMPT_TOKENS = 2_048
_USD_MICROS = Decimal(1_000_000)
_SAFE_ROUTING_KEYS = frozenset(
    {
        "allow_fallbacks",
        "attempt",
        "order",
        "only",
        "provider",
        "require_parameters",
        "sort",
        "strategy",
        "zdr",
    }
)


class ProbeRunMode(StrEnum):
    """Whether the probe transport is deterministic or may spend real credit."""

    FAKE = "fake"
    LIVE = "live"


class CompatibilityProbeError(ValueError):
    """A stable, safe denial while preparing or running a probe."""


@dataclass(frozen=True, slots=True)
class ProbeCase:
    """One canonical role/action observation and its required response."""

    case_id: str
    observation: Observation
    expected_action: Action
    requires_public_message: bool = False


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """One no-secret transport request built from a canonical probe case."""

    case: ProbeCase
    canonical_slug: str
    prompt_hash: str
    schema_hash: str
    expected_provider_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ProbeTransportResult:
    """Compact transport result before it is hashed/sanitized for storage."""

    status: str
    raw_response: str | None
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_micros: int | None = None
    actual_model: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    generation_id: str | None = None
    routing: Mapping[str, object] | None = None
    strict_schema_observed: bool | None = None
    provider_fingerprint: str | None = None


class OpenRouterProbeTransport(Protocol):
    """Stub-friendly OpenRouter completion boundary used by a probe runner."""

    async def complete(self, request: ProbeRequest) -> ProbeTransportResult: ...


@dataclass(frozen=True, slots=True)
class ProbeRunResult:
    """Persisted compatibility result returned to a CLI, job, or admin route."""

    evidence: ModelProbeEvidence
    created: bool
    status: str

    def operator_summary(self) -> dict[str, object]:
        """Return a credential-free operator response shape."""
        payload = self.evidence.evidence
        return {
            "catalog_content_hash": payload["catalog_content_hash"],
            "canonical_slug": payload["canonical_slug"],
            "created": self.created,
            "observed_at": _as_utc(self.evidence.observed_at).isoformat().replace("+00:00", "Z"),
            "status": self.status,
            "suite_version": self.evidence.probe_suite_version,
            "tokens_used": payload["tokens_used"],
            "latency_ms": payload["latency_ms"],
            "cost_micros": payload["cost_micros"],
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def probe_prompt_hash() -> str:
    """Return the stable hash of the probe-only prompt contract."""
    return _hash({"prompt": _PROBE_SYSTEM_PROMPT, "version": DEFAULT_PROBE_SUITE_VERSION})


def probe_schema_hash() -> str:
    """Return the stable hash of the AgentResponse JSON schema sent to providers."""
    return _hash(AgentResponse.model_json_schema())


def _suite_version_for_mode(mode: ProbeRunMode) -> str:
    """Separate no-spend fake evidence from explicitly funded live evidence."""
    if mode is ProbeRunMode.FAKE:
        return DEFAULT_PROBE_SUITE_VERSION
    return f"{DEFAULT_PROBE_SUITE_VERSION}_{mode.value}"


def _observation(
    *,
    phase: str,
    role: Role,
    faction: Faction,
    legal_actions: Sequence[ActionType],
    targets: Sequence[str],
) -> Observation:
    return Observation(
        ruleset_id="compatibility_probe_v1",
        game_public_id="compatibility-probe",
        phase=phase,
        day=1,
        round=1,
        you=YouInfo(player_id="P01", alive=True, role=role, faction=faction),
        alive_players=("P01", "P02", "P03"),
        dead_players=(),
        public_events=(),
        private_events=(),
        legal_actions=LegalActions(
            allowed_action_types=list(legal_actions),
            legal_targets=list(targets),
        ),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=600,
            private_message_max_chars=600,
            memory_update_max_chars=1200,
        ),
    )


def canonical_probe_cases() -> tuple[ProbeCase, ...]:
    """Return the complete static compatibility suite for every action family."""
    discussion = _observation(
        phase="DAY_1_DISCUSSION_ROUND_1",
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        legal_actions=(ActionType.NOOP,),
        targets=(),
    )
    vote = _observation(
        phase="DAY_1_VOTE",
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        legal_actions=(ActionType.VOTE, ActionType.ABSTAIN),
        targets=("P02", "P03"),
    )
    night_cases = (
        ("mafia_kill", Role.MAFIA_GOON, Faction.MAFIA, ActionType.MAFIA_KILL),
        ("protect", Role.DOCTOR, Faction.TOWN, ActionType.PROTECT),
        ("investigate", Role.DETECTIVE, Faction.TOWN, ActionType.INVESTIGATE),
        ("roleblock", Role.MAFIA_ROLEBLOCKER, Faction.MAFIA, ActionType.ROLEBLOCK),
        ("frame", Role.FRAMER, Faction.MAFIA, ActionType.FRAME),
        ("track", Role.TRACKER, Faction.TOWN, ActionType.TRACK),
        ("watch", Role.WATCHER, Faction.TOWN, ActionType.WATCH),
        ("clean", Role.JANITOR, Faction.MAFIA, ActionType.CLEAN),
        ("serial_kill", Role.SERIAL_KILLER, Faction.SERIAL_KILLER, ActionType.SERIAL_KILL),
    )
    output = [
        ProbeCase(
            case_id="json_contract",
            observation=discussion,
            expected_action=Action(type=ActionType.NOOP),
        ),
        ProbeCase(
            case_id="public_message",
            observation=discussion,
            expected_action=Action(type=ActionType.NOOP),
            requires_public_message=True,
        ),
        ProbeCase(
            case_id="vote",
            observation=vote,
            expected_action=Action(type=ActionType.VOTE, target="P02"),
        ),
    ]
    for case_id, role, faction, action_type in night_cases:
        output.append(
            ProbeCase(
                case_id=case_id,
                observation=_observation(
                    phase="NIGHT_1_ACTIONS",
                    role=role,
                    faction=faction,
                    legal_actions=(action_type,),
                    targets=("P02", "P03"),
                ),
                expected_action=Action(type=action_type, target="P02"),
            )
        )
    return tuple(output)


class DeterministicFakeProbeTransport:
    """Deterministic no-network transport used by default probes and tests."""

    def __init__(
        self,
        *,
        failure_case_id: str | None = None,
        failure_status: str = "provider_error",
    ) -> None:
        self.failure_case_id = failure_case_id
        self.failure_status = failure_status
        self.requests: list[ProbeRequest] = []

    async def complete(self, request: ProbeRequest) -> ProbeTransportResult:
        self.requests.append(request)
        if request.case.case_id == self.failure_case_id:
            return ProbeTransportResult(
                status=self.failure_status,
                raw_response=None,
                latency_ms=7,
                actual_model=request.canonical_slug,
                provider="deterministic-fake",
                endpoint="https://fake.openrouter.invalid/api/v1/chat/completions",
                routing={"strategy": "deterministic", "allow_fallbacks": False},
                strict_schema_observed=False,
                provider_fingerprint=request.expected_provider_fingerprint or "fake-v1",
            )
        response = AgentResponse(
            public_message=(
                "This is one canonical public message."
                if request.case.requires_public_message
                else None
            ),
            private_message=None,
            action=request.case.expected_action,
            memory_update="probe-memory",
            rationale_summary=None,
        )
        return ProbeTransportResult(
            status="success",
            raw_response=response.model_dump_json(),
            latency_ms=11,
            input_tokens=120,
            output_tokens=40,
            cost_micros=0,
            actual_model=request.canonical_slug,
            provider="deterministic-fake",
            endpoint="https://fake.openrouter.invalid/api/v1/chat/completions",
            generation_id=f"fake-{request.case.case_id}",
            routing={"strategy": "deterministic", "allow_fallbacks": False},
            strict_schema_observed=True,
            provider_fingerprint=request.expected_provider_fingerprint or "fake-v1",
        )


class HttpxOpenRouterProbeTransport:
    """Live OpenRouter JSON-schema transport with a non-serializable key."""

    __slots__ = ("_api_key", "_base_url", "_client")

    def __init__(self, *, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def __repr__(self) -> str:
        return "HttpxOpenRouterProbeTransport(api_key=[redacted])"

    async def complete(self, request: ProbeRequest) -> ProbeTransportResult:
        started = time.monotonic()
        payload = {
            "model": request.canonical_slug,
            "messages": [
                {"role": "system", "content": _PROBE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": request.case.observation.model_dump_json(),
                },
            ],
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "padrino_agent_response",
                    "strict": True,
                    "schema": AgentResponse.model_json_schema(),
                },
            },
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            if response.status_code >= 400:
                return ProbeTransportResult(
                    status="provider_error", raw_response=None, latency_ms=latency_ms
                )
            decoded = response.json()
        except (httpx.HTTPError, ValueError):
            return ProbeTransportResult(
                status="provider_error",
                raw_response=None,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        if not isinstance(decoded, Mapping):
            return ProbeTransportResult(
                status="provider_error", raw_response=None, latency_ms=latency_ms
            )
        text, result_status = _response_text(decoded)
        usage = decoded.get("usage")
        usage_mapping = usage if isinstance(usage, Mapping) else {}
        provider = _safe_text(decoded.get("provider"))
        actual_model = _safe_text(decoded.get("model"))
        endpoint = _safe_endpoint(self._base_url)
        return ProbeTransportResult(
            status=result_status,
            raw_response=text,
            latency_ms=latency_ms,
            input_tokens=_safe_int(usage_mapping.get("prompt_tokens")),
            output_tokens=_safe_int(usage_mapping.get("completion_tokens")),
            cost_micros=_cost_micros(usage_mapping.get("cost")),
            actual_model=actual_model,
            provider=provider,
            endpoint=endpoint,
            generation_id=_safe_text(decoded.get("id")),
            routing=_safe_routing(decoded.get("provider")),
            strict_schema_observed=True,
            provider_fingerprint=_hash(
                {"actual_model": actual_model, "endpoint": endpoint, "provider": provider}
            ),
        )


def _response_text(payload: Mapping[str, object]) -> tuple[str | None, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "empty_content"
    first = choices[0]
    if not isinstance(first, Mapping):
        return None, "empty_content"
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None, "empty_content"
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content, "success"
    if isinstance(message.get("refusal"), str):
        return None, "refusal"
    return None, "empty_content"


def _safe_text(value: object, *, max_length: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost_micros(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return int((parsed * _USD_MICROS).to_integral_value(rounding=ROUND_CEILING))


def _safe_endpoint(value: object) -> str | None:
    text = _safe_text(value, max_length=2_048)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"


def _safe_routing(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, object] = {}
    for key, nested in value.items():
        if not isinstance(key, str) or key not in _SAFE_ROUTING_KEYS:
            continue
        if (isinstance(nested, str) and len(nested) <= 256) or isinstance(nested, (bool, int)):
            output[key] = nested
        elif isinstance(nested, list) and all(
            isinstance(item, str) and len(item) <= 128 for item in nested
        ):
            output[key] = list(nested)
    return output


def _provider_fingerprint(entry: ModelCatalogEntry) -> str | None:
    for source in (entry.provider_summary, entry.raw_metadata):
        value = source.get("provider_fingerprint")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _estimated_live_cost_micros(entry: ModelCatalogEntry) -> int | None:
    prompt = _safe_int(entry.pricing_micros.get("prompt_per_million"))
    completion = _safe_int(entry.pricing_micros.get("completion_per_million"))
    if prompt is None or completion is None:
        return None
    numerator = _MAX_PROMPT_TOKENS * prompt + _MAX_OUTPUT_TOKENS * completion
    per_case = (numerator + 999_999) // 1_000_000
    return per_case * len(canonical_probe_cases())


class CompatibilityProbeRunner:
    """Run one bounded probe suite and append a single immutable evidence row."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        transport: OpenRouterProbeTransport,
        now: Callable[[], datetime],
        trigger_rate_limit_per_minute: int,
        operator_cost_cap_micros: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._now = now
        self._trigger_rate_limit_per_minute = trigger_rate_limit_per_minute
        self._operator_cost_cap_micros = operator_cost_cap_micros
        self._monotonic = monotonic
        self._locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        *,
        catalog_content_hash: str,
        canonical_slug: str,
        mode: ProbeRunMode,
        trigger_key: str,
        spend_cap_micros: int | None = None,
    ) -> ProbeRunResult:
        """Run or reuse the exact model/catalog/prompt/schema compatibility probe."""
        prompt_hash = probe_prompt_hash()
        schema_hash = probe_schema_hash()
        suite_version = _suite_version_for_mode(mode)
        key = _hash(
            {
                "catalog_content_hash": catalog_content_hash,
                "canonical_slug": canonical_slug,
                "prompt_hash": prompt_hash,
                "schema_hash": schema_hash,
                "suite": suite_version,
            }
        )
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            snapshot, entry = await self._resolve_catalog(catalog_content_hash, canonical_slug)
            self._validate_budget(entry, mode=mode, spend_cap_micros=spend_cap_micros)
            async with self._session_factory() as session:
                existing = await get_probe_evidence_by_key(
                    session,
                    catalog_snapshot_id=snapshot.id,
                    model_catalog_entry_id=entry.id,
                    prompt_hash=prompt_hash,
                    schema_hash=schema_hash,
                    probe_suite_version=suite_version,
                )
            if existing is not None:
                return ProbeRunResult(evidence=existing, created=False, status=existing.status)

            await self._enforce_trigger_rate_limit(trigger_key)
            evidence_payload, status = await self._execute_suite(entry, canonical_slug)
            observed_at = _as_utc(self._now())
            async with self._session_factory() as session:
                row, created = await create_or_get_probe_evidence(
                    session,
                    catalog_snapshot_id=snapshot.id,
                    model_catalog_entry_id=entry.id,
                    prompt_hash=prompt_hash,
                    schema_hash=schema_hash,
                    probe_suite_version=suite_version,
                    status=status,
                    evidence={
                        "catalog_content_hash": snapshot.content_hash,
                        "canonical_slug": canonical_slug,
                        "estimated_cost_micros": _estimated_live_cost_micros(entry),
                        **evidence_payload,
                    },
                    observed_at=observed_at,
                )
                await session.commit()
            return ProbeRunResult(evidence=row, created=created, status=row.status)

    async def _resolve_catalog(
        self, catalog_content_hash: str, canonical_slug: str
    ) -> tuple[CatalogSnapshot, ModelCatalogEntry]:
        async with self._session_factory() as session:
            snapshot = await session.scalar(
                select(CatalogSnapshot).where(
                    CatalogSnapshot.content_hash == catalog_content_hash,
                    CatalogSnapshot.status == "COMPLETE",
                )
            )
            if snapshot is None:
                raise CompatibilityProbeError("probe_catalog_snapshot_not_found")
            entry = await session.scalar(
                select(ModelCatalogEntry).where(
                    ModelCatalogEntry.catalog_snapshot_id == snapshot.id,
                    ModelCatalogEntry.canonical_slug == canonical_slug,
                )
            )
            if entry is None:
                raise CompatibilityProbeError("probe_model_not_in_catalog_snapshot")
            return snapshot, entry

    def _validate_budget(
        self,
        entry: ModelCatalogEntry,
        *,
        mode: ProbeRunMode,
        spend_cap_micros: int | None,
    ) -> None:
        if self._trigger_rate_limit_per_minute <= 0:
            raise CompatibilityProbeError("probe_trigger_rate_limited")
        if self._operator_cost_cap_micros < 0:
            raise CompatibilityProbeError("probe_operator_cost_cap_invalid")
        if mode is ProbeRunMode.FAKE:
            return
        if spend_cap_micros is None or spend_cap_micros <= 0:
            raise CompatibilityProbeError("probe_spend_cap_required")
        if spend_cap_micros > self._operator_cost_cap_micros:
            raise CompatibilityProbeError("probe_spend_cap_exceeds_operator_limit")
        estimated = _estimated_live_cost_micros(entry)
        if estimated is None:
            raise CompatibilityProbeError("probe_price_unknown")
        if estimated > spend_cap_micros:
            raise CompatibilityProbeError("probe_spend_cap_too_low")

    async def _enforce_trigger_rate_limit(self, trigger_key: str) -> None:
        key_hash = hashlib.sha256(f"openrouter-probe:{trigger_key}".encode()).hexdigest()
        decision = await DatabaseRateLimitStore(self._session_factory).record_request(
            key_hash,
            now=self._monotonic(),
            limit_per_minute=self._trigger_rate_limit_per_minute,
        )
        if not decision.allowed:
            raise CompatibilityProbeError("probe_trigger_rate_limited")

    async def _execute_suite(
        self, entry: ModelCatalogEntry, canonical_slug: str
    ) -> tuple[dict[str, object], str]:
        cases: list[dict[str, object]] = []
        failed_case_ids: list[str] = []
        tokens_used = 0
        latency_ms = 0
        cost_micros = 0
        strict_schema_observed = True
        fingerprints: list[str] = []
        for case in canonical_probe_cases():
            result = await self._transport.complete(
                ProbeRequest(
                    case=case,
                    canonical_slug=canonical_slug,
                    prompt_hash=probe_prompt_hash(),
                    schema_hash=probe_schema_hash(),
                    expected_provider_fingerprint=_provider_fingerprint(entry),
                )
            )
            passed = _case_passed(case, result)
            if not passed:
                failed_case_ids.append(case.case_id)
            tokens_used += (result.input_tokens or 0) + (result.output_tokens or 0)
            latency_ms += max(0, result.latency_ms)
            cost_micros += result.cost_micros or 0
            strict_schema_observed = (
                strict_schema_observed and result.strict_schema_observed is True
            )
            if result.provider_fingerprint is not None:
                fingerprints.append(result.provider_fingerprint)
            cases.append(_case_evidence(case, result, passed=passed))
        expected_fingerprint = _provider_fingerprint(entry)
        drift_resolved = (
            bool(fingerprints)
            and not failed_case_ids
            and (
                expected_fingerprint is None
                or all(value == expected_fingerprint for value in fingerprints)
            )
        )
        return (
            {
                "cases": cases,
                "cost_micros": cost_micros,
                "drift_resolved": drift_resolved,
                "failed_case_ids": failed_case_ids,
                "latency_ms": latency_ms,
                "provider_fingerprint": expected_fingerprint,
                "schema_coercion_succeeded": not failed_case_ids,
                "strict_schema_observed": strict_schema_observed,
                "tokens_used": tokens_used,
            },
            "success" if not failed_case_ids else "failed",
        )


def _case_passed(case: ProbeCase, result: ProbeTransportResult) -> bool:
    if result.status != "success" or result.raw_response is None:
        return False
    parsed = parse_agent_response(result.raw_response)
    if not isinstance(parsed, AgentResponse):
        return False
    if parsed.action != case.expected_action:
        return False
    return not case.requires_public_message or bool(
        parsed.public_message and parsed.public_message.strip()
    )


def _case_evidence(
    case: ProbeCase, result: ProbeTransportResult, *, passed: bool
) -> dict[str, object]:
    result_hash = _hash(
        result.raw_response
        if result.raw_response is not None
        else {"case_id": case.case_id, "status": result.status}
    )
    return {
        "actual_model": _safe_text(result.actual_model),
        "case_id": case.case_id,
        "cost_micros": result.cost_micros if result.cost_micros is not None else 0,
        "endpoint": _safe_endpoint(result.endpoint),
        "generation_id": _safe_text(result.generation_id),
        "input_tokens": result.input_tokens if result.input_tokens is not None else 0,
        "latency_ms": max(0, result.latency_ms),
        "output_tokens": result.output_tokens if result.output_tokens is not None else 0,
        "passed": passed,
        "provider": _safe_text(result.provider),
        "result_hash": result_hash,
        "routing": _safe_routing(result.routing),
        "status": result.status,
    }


__all__ = [
    "DEFAULT_PROBE_SUITE_VERSION",
    "CompatibilityProbeError",
    "CompatibilityProbeRunner",
    "DeterministicFakeProbeTransport",
    "HttpxOpenRouterProbeTransport",
    "OpenRouterProbeTransport",
    "ProbeCase",
    "ProbeRequest",
    "ProbeRunMode",
    "ProbeRunResult",
    "ProbeTransportResult",
    "canonical_probe_cases",
    "probe_prompt_hash",
    "probe_schema_hash",
]
