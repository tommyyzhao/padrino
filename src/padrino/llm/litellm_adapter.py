"""LiteLLM-backed :class:`LlmAdapter` with primary/fallback routing.

`LiteLlmAdapter` issues a :func:`litellm.acompletion` call against the
:attr:`RoutingPolicy.primary_model`, retrying transient errors. If the call still
fails (network error, rate limit, timeout, etc.), the adapter iterates
:attr:`RoutingPolicy.same_model_hosts` (US-079) — alternate provider endpoints
serving the SAME upstream weights — before falling through to a configured
:attr:`RoutingPolicy.fallback_model` (different model). The final
:class:`AdapterResult` returned to the caller carries the routing status (``ok``,
``same_model_fallback_ok``, ``fallback_ok``, or ``exhausted`` when every target
failed) and every provider attempt in :attr:`AdapterResult.attempts` for the
recording layer to mirror as separate ``llm_calls`` rows. Provider failures carry a
``failure_reason`` (``TIMEOUT`` / ``PROVIDER_ERROR``) the runner turns into a failure
event.

Parse failures (invalid JSON, schema violations) never trigger fallback — they are not
provider-side errors. They get ``padrino_llm_reask_invalid`` re-asks on the same
target; if still unusable the runner coerces them to a safe action via
:func:`padrino.core.agents.coercion.coerce_response_failure`.

Impure module: lives in the ``llm`` layer and is never imported by pure-core.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import litellm
import structlog
from litellm.exceptions import Timeout as LiteLlmTimeout

from padrino.core.agents.contract import (
    AgentResponse,
    ResponseError,
    _strip_think,
    parse_agent_response,
)
from padrino.core.engine.actions import Action
from padrino.core.engine.rng import SeededRng
from padrino.core.enums import ActionType, Role
from padrino.core.observations import Observation
from padrino.llm.adapter import (
    ERROR_TRUNCATED,
    AdapterResult,
    AdapterStatus,
    AgentBuild,
    RoutingPolicy,
    SameModelHost,
)
from padrino.llm.retry import (
    LlmCallFailed,
    RetryPolicy,
    default_retry_policy,
    with_retry,
)
from padrino.llm.secrets import resolve_secret
from padrino.observability.metrics import record_llm_call
from padrino.openrouter.routing_metadata import OpenRouterCallAudit, extract_openrouter_call_audit
from padrino.settings import get_settings

_logger = structlog.get_logger("padrino.llm")

# Share of ``timeout_s`` the adapter budgets for itself; the rest is slack so the runner's
# outer hard deadline never races the adapter's own last attempt.
_BUDGET_FRACTION: Final[float] = 0.9
_MIN_ATTEMPT_S: Final[float] = 0.01
_REASK_INSTRUCTION: Final[str] = (
    "Your previous reply was not usable. Reply with only the JSON object matching the schema."
)

DEFAULT_SYSTEM_PROMPT: Final[str] = (
    "You are a player in a hidden-role social-deduction game. "
    "Your goal is to make your faction win.\n"
    "You may deceive in public chat if strategically useful, but mechanical "
    "actions are expressed only through the JSON `action` field.\n"
    "Public messages are visible to every living player. Private messages are "
    "visible only when the observation lists a private channel for this phase.\n"
    "Your `memory_update` is private and will be shown only to you in later "
    "phases of this same game; you have no persistent identity across games.\n"
    "Respond with a single JSON object matching this schema exactly:\n"
    '  {"public_message": str|null, "private_message": str|null,'
    ' "action": {"type": str, "target": str|null}, "memory_update": str,'
    ' "rationale_summary": str|null}\n'
    "Do not include any text outside the JSON object."
)


def build_messages(
    observation: Observation, *, system_prompt: str = DEFAULT_SYSTEM_PROMPT
) -> list[dict[str, str]]:
    """Render the observation into a system+user chat-completions payload."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": observation.model_dump_json()},
    ]


@dataclass(frozen=True, slots=True)
class _Hop:
    """One routing target: the primary, a same-model host, or the different-model fallback."""

    model_id: str
    ok_status: AdapterStatus
    api_base: str | None = None
    auth_secret: str | None = None
    extra_headers: Mapping[str, str] | None = None


class LiteLlmAdapter:
    """LiteLLM-backed adapter with primary→fallback routing.

    ``timeout_s`` is the TOTAL budget for one :meth:`complete` call (the runner's
    phase deadline). The adapter spends ~90% of it: each hop gets an equal share, each
    retry attempt inside a hop gets ``remaining / attempts_left``, and a re-ask uses what
    is left, so retries always fit inside the deadline the runner enforces on the outside.

    ponytail: no per-gateway concurrency semaphore; add one when 429s from a shared
    gateway are observed under a parallel batch.
    """

    __slots__ = (
        "_api_base",
        "_auth_secret",
        "_build",
        "_extra_headers",
        "_policy",
        "_reask_invalid",
        "_retry_policy",
        "_same_model_hosts",
        "_sleeper",
        "_system_prompt",
        "_system_prompts_by_role",
        "_timeout_s",
    )

    def __init__(
        self,
        *,
        routing_policy: RoutingPolicy,
        agent_build: AgentBuild,
        timeout_s: float,
        auth_secret_ref: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        system_prompts_by_role: Mapping[Role, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        reask_invalid: int | None = None,
    ) -> None:
        # Resolve deployment references once at construction so a misconfigured
        # provider fails loudly at boot instead of silently 401-ing on first
        # call. Player-owned BYOK values arrive as ``api_key`` only from the
        # encrypted credential boundary and remain in this impure object.
        if (auth_secret_ref is None) == (api_key is None):
            raise ValueError("exactly one adapter credential source is required")
        if api_key is not None:
            if not api_key:
                raise ValueError("adapter API key must not be empty")
            self._auth_secret = api_key
        else:
            assert auth_secret_ref is not None
            self._auth_secret = resolve_secret(auth_secret_ref)
        self._api_base = api_base
        # Extra request headers for the PRIMARY call only (e.g. opting a local gateway out of
        # prompt injection); merged over, never replacing, OpenRouter headers.
        self._extra_headers = dict(extra_headers) if extra_headers else None
        self._policy = routing_policy
        self._build = agent_build
        if routing_policy.openrouter is not None and api_base is None:
            raise ValueError("OpenRouter adapters require an explicit api_base")
        # Resolve each same-model host's credential at construction too, so a
        # misconfigured Z.AI key surfaces at boot rather than only when the
        # primary host happens to fail. The resolved value is cached alongside
        # the host descriptor to keep _call_model's per-call path allocation-free.
        self._same_model_hosts: tuple[tuple[SameModelHost, str], ...] = tuple(
            (host, resolve_secret(host.auth_secret_ref)) for host in routing_policy.same_model_hosts
        )
        self._system_prompt = system_prompt
        # When ``system_prompts_by_role`` is provided (canonical-prompt path,
        # US-052) the per-call prompt is looked up by ``observation.you.role``.
        # Otherwise every seat gets the same ``system_prompt`` — preserves the
        # pre-US-052 behaviour for tests and the v1 mock harness.
        self._system_prompts_by_role: Mapping[Role, str] | None = (
            dict(system_prompts_by_role) if system_prompts_by_role is not None else None
        )
        self._timeout_s = timeout_s
        # Retry seam (US-053): bounded exponential backoff with injectable
        # sleeper so tests pin time. ``sleeper`` defaults to ``asyncio.sleep``.
        self._retry_policy = retry_policy if retry_policy is not None else default_retry_policy()
        self._sleeper: Callable[[float], Awaitable[None]] = (
            sleeper if sleeper is not None else asyncio.sleep
        )
        self._reask_invalid = (
            reask_invalid if reask_invalid is not None else get_settings().padrino_llm_reask_invalid
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        hops = [
            _Hop(
                self._policy.primary_model,
                "ok",
                api_base=self._api_base,
                auth_secret=(
                    self._auth_secret
                    if self._api_base is not None or self._policy.openrouter is not None
                    else None
                ),
                extra_headers=self._extra_headers,
            ),
            # Same-model alternate hosts (US-079) serve the SAME weights, so the
            # leaderboard still credits the AgentBuild's canonical model identity.
            *(
                _Hop(
                    host.litellm_model_id,
                    "same_model_fallback_ok",
                    api_base=host.api_base,
                    auth_secret=secret,
                )
                for host, secret in self._same_model_hosts
            ),
        ]
        if self._policy.fallback_model is not None:
            hops.append(
                _Hop(
                    self._policy.fallback_model,
                    "fallback_ok",
                    api_base=self._api_base if self._policy.openrouter is not None else None,
                    auth_secret=self._auth_secret if self._policy.openrouter is not None else None,
                )
            )

        deadline = time.monotonic() + self._timeout_s * _BUDGET_FRACTION
        rows: list[AdapterResult] = []  # every provider attempt before the final result
        failed_outcome: AdapterResult | None = None
        for index, hop in enumerate(hops):
            now = time.monotonic()
            hop_deadline = now + max(deadline - now, 0.0) / (len(hops) - index)
            hop_rows, outcome = await self._call_hop(observation, hop, hop_deadline, deadline)
            if outcome.failure_reason is None:
                # A reply came back (even an unparseable one): parse failures are the
                # runner's to coerce, never a reason to hop to another model.
                final = outcome
                if hop.ok_status != "ok":
                    final = outcome.model_copy(update={"status": hop.ok_status})
                rows.extend(row for row in hop_rows if row is not outcome)
                return final.model_copy(update={"attempts": tuple(rows)})
            # Provider failure: the hop's outcome is its last attempt. Demote it so the
            # per-attempt rows record the routing decision.
            rows.extend(hop_rows[:-1])
            failed_outcome = outcome
            if index < len(hops) - 1:
                rows.append(outcome.model_copy(update={"status": "primary_failed"}))

        assert failed_outcome is not None
        terminal = _with_coerced_response(
            failed_outcome.model_copy(update={"status": "exhausted"}), observation
        )
        return terminal.model_copy(update={"attempts": tuple(rows)})

    def _system_prompt_for(self, observation: Observation) -> str:
        if self._system_prompts_by_role is None:
            return self._system_prompt
        return self._system_prompts_by_role.get(observation.you.role, self._system_prompt)

    async def _call_hop(
        self,
        observation: Observation,
        hop: _Hop,
        hop_deadline: float,
        deadline: float,
    ) -> tuple[list[AdapterResult], AdapterResult]:
        """Call one target (with transient retries) and, if the reply is unusable, re-ask.

        Returns every attempt made and the one that represents the hop's outcome: the last
        reply, or the last failure when no reply ever came back.
        """
        messages = build_messages(observation, system_prompt=self._system_prompt_for(observation))
        rows = await self._call_model(observation, hop, messages, hop_deadline)
        outcome = rows[-1]
        for _ in range(self._reask_invalid):
            if (
                outcome.failure_reason is not None
                or outcome.error == ERROR_TRUNCATED
                or isinstance(outcome.parsed_response, AgentResponse)
                or time.monotonic() >= deadline
            ):
                break
            messages = [
                *messages,
                {"role": "assistant", "content": outcome.raw_response},
                {"role": "user", "content": _REASK_INSTRUCTION},
            ]
            more = await self._call_model(observation, hop, messages, deadline)
            rows.extend(more)
            if more[-1].failure_reason is not None:
                break  # the unusable first reply stays the outcome; the re-ask just failed
            outcome = more[-1]
        return rows, outcome

    async def _call_model(
        self,
        observation: Observation,
        hop: _Hop,
        messages: list[dict[str, str]],
        deadline: float,
    ) -> list[AdapterResult]:
        """Issue one logical call; return every provider attempt, the last being the outcome.

        Failed transient attempts precede the final row, so the recording layer can persist
        one ``llm_calls`` row per real request.
        """
        model_id = hop.model_id
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            **self._build.inference_params,
        }
        # Per-host overrides (US-079 / US-082): same-model alternate hosts and
        # OpenAI-compatible primary providers may supply ``api_base`` plus an
        # explicit credential. Primary calls without ``api_base`` keep using
        # LiteLLM's provider-specific env var defaults.
        if hop.api_base is not None:
            kwargs["api_base"] = hop.api_base
        if hop.auth_secret is not None:
            kwargs["api_key"] = hop.auth_secret
        if self._policy.openrouter is not None:
            kwargs["custom_llm_provider"] = "openrouter"
            kwargs["extra_headers"] = self._policy.openrouter.request_headers()
            kwargs["extra_body"] = {
                "provider": self._policy.openrouter.provider.as_request_dict(),
            }
        if hop.extra_headers:
            kwargs["extra_headers"] = {**kwargs.get("extra_headers", {}), **hop.extra_headers}
        # Deterministic jitter seed: bind to the observation contents so a
        # replay of the same observation produces an identical backoff schedule.
        rng = SeededRng(f"{observation.phase}:{observation.you.player_id}:{model_id}")
        max_attempts = self._retry_policy.max_attempts
        failed: list[AdapterResult] = []

        async def _call() -> tuple[Any, int]:
            attempts_left = max_attempts - len(failed)
            per_attempt = max((deadline - time.monotonic()) / attempts_left, _MIN_ATTEMPT_S)
            start = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    litellm.acompletion(**kwargs, timeout=per_attempt), timeout=per_attempt
                )
                return response, _elapsed_ms(start)
            except Exception as exc:
                failed.append(
                    self._failure_row(
                        exc,
                        model_id=model_id,
                        latency_ms=_elapsed_ms(start),
                        auth_secret=hop.auth_secret,
                        attempts=len(failed) + 1,
                    )
                )
                raise

        try:
            (response, latency_ms), _attempt_history = await with_retry(
                _call,
                self._retry_policy,
                sleeper=self._sleeper,
                rng=rng,
            )
        except Exception:
            # The failed attempts (the last one terminal) are already recorded in ``failed``.
            return failed

        return [*failed, self._reply_row(response, model_id=model_id, latency_ms=latency_ms)]

    def _failure_row(
        self,
        exc: BaseException,
        *,
        model_id: str,
        latency_ms: int,
        auth_secret: str | None,
        attempts: int,
    ) -> AdapterResult:
        error_kind = type(exc).__name__
        error_message = _redact_error_message(str(exc), auth_secret)
        _log_provider_failure(model_id=model_id, error_kind=error_kind, error_message=error_message)
        record_llm_call(model_id=model_id, status="provider_error", latency_ms=latency_ms)
        return AdapterResult(
            raw_response="",
            parsed_response=ResponseError(
                reason="SCHEMA_VIOLATION",
                details=f"{error_kind}: {error_message}",
            ),
            latency_ms=latency_ms,
            model_id=model_id,
            status="provider_error",
            error=f"{error_kind}: {error_message}",
            failure=LlmCallFailed(
                error_kind=error_kind, error_message=error_message, attempts=attempts
            ),
            failure_reason="TIMEOUT" if _is_timeout(exc) else "PROVIDER_ERROR",
            openrouter_audit=self._openrouter_audit(None, requested_model_slug=model_id),
        )

    def _reply_row(self, response: Any, *, model_id: str, latency_ms: int) -> AdapterResult:
        # ``raw_response`` is the model's original text; the contract parser itself
        # tolerates think blocks, code fences and prose around the JSON object.
        text = _extract_text(response)
        parsed = parse_agent_response(text)
        status: AdapterStatus
        error: str | None = None
        if isinstance(parsed, AgentResponse):
            status = "ok"
        else:
            status = "invalid_json" if parsed.reason == "INVALID_JSON" else "schema_violation"
            finish_reason = _extract_finish_reason(response)
            if _is_truncated(text, finish_reason):
                # Reasoning models can spend all of max_tokens thinking; say so instead
                # of reporting a generic parse failure. ``reasoning_content`` is never
                # used as the answer.
                status, error = "invalid_json", ERROR_TRUNCATED
                parsed = ResponseError(
                    reason="INVALID_JSON", details=f"truncated (finish_reason={finish_reason})"
                )
            _log_unusable_reply(
                model_id=model_id,
                outcome=error or status,
                finish_reason=finish_reason,
                output_tokens=_extract_output_tokens(response),
                reasoning_tokens=_extract_reasoning_tokens(response),
            )
        record_llm_call(model_id=model_id, status=status, latency_ms=latency_ms)
        return AdapterResult(
            raw_response=text,
            parsed_response=parsed,
            latency_ms=latency_ms,
            input_tokens=_extract_input_tokens(response),
            output_tokens=_extract_output_tokens(response),
            cost_usd=_extract_cost(response),
            model_id=model_id,
            provider_response_id=_extract_response_id(response),
            status=status,
            error=error,
            openrouter_audit=self._openrouter_audit(response, requested_model_slug=model_id),
        )

    def _openrouter_audit(
        self, response: object | None, *, requested_model_slug: str
    ) -> OpenRouterCallAudit | None:
        """Return the safe OpenRouter audit snapshot, if this adapter uses it."""
        openrouter = self._policy.openrouter
        if openrouter is None:
            return None
        return extract_openrouter_call_audit(
            response,
            requested_model_slug=requested_model_slug,
            provider_preferences=openrouter.provider.as_request_dict(),
            catalog_snapshot_id=self._build.catalog_snapshot_id,
        )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _redact_error_message(message: str, secret: str | None) -> str:
    """Remove credentials and credential headers from provider diagnostics."""
    redacted = message
    if secret:
        redacted = redacted.replace(secret, "[redacted]")
    return re.sub(
        r"(?i)\b(?:authorization|x-api-key|api-key)\s*:\s*[^,;\r\n]+",
        "[credential header redacted]",
        redacted,
    )


def _log_provider_failure(*, model_id: str, error_kind: str, error_message: str) -> None:
    """Emit a redacted provider failure without request/credential fields."""
    _logger.warning(
        "llm.provider_failure",
        model_id=model_id,
        error_kind=error_kind,
        error_message=error_message,
    )


def _extract_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _extract_finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    value = getattr(choices[0], "finish_reason", None)
    return value if isinstance(value, str) else None


def _is_truncated(text: str, finish_reason: str | None) -> bool:
    """Unparseable reply that was cut off, or left empty once reasoning is stripped."""
    # ponytail: reuses the contract's private think-stripper so both layers agree on
    # what counts as reasoning; promote it to a public name if a third caller appears.
    return finish_reason == "length" or not _strip_think(text).strip()


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError | LiteLlmTimeout)


def _extract_reasoning_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _log_unusable_reply(
    *,
    model_id: str,
    outcome: str,
    finish_reason: str | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
) -> None:
    fields: dict[str, Any] = {
        "model_id": model_id,
        "outcome": outcome,
        "finish_reason": finish_reason,
        "output_tokens": output_tokens,
    }
    if reasoning_tokens is not None:
        fields["reasoning_tokens"] = reasoning_tokens
    _logger.warning("llm.unusable_reply", **fields)


def _extract_input_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    value = getattr(usage, "prompt_tokens", None)
    return int(value) if isinstance(value, int) else None


def _extract_output_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    value = getattr(usage, "completion_tokens", None)
    return int(value) if isinstance(value, int) else None


def _extract_cost(response: Any) -> float | None:
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        cost = _as_cost(hidden.get("response_cost"))
        if cost is not None:
            return cost
    usage = getattr(response, "usage", None)
    if isinstance(usage, Mapping):
        return _as_cost(usage.get("cost"))
    return _as_cost(getattr(usage, "cost", None))


def _as_cost(value: object) -> float | None:
    """Normalize a LiteLLM/OpenRouter cost field without accepting booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _extract_response_id(response: Any) -> str | None:
    value = getattr(response, "id", None)
    return value if isinstance(value, str) else None


def _coerced_safe_response(observation: Observation) -> AgentResponse:
    if observation.phase.endswith("_VOTE"):
        action = Action(type=ActionType.ABSTAIN, target=None)
    else:
        action = Action(type=ActionType.NOOP, target=None)
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=action,
        memory_update="",
        rationale_summary=None,
    )


def _with_coerced_response(base: AdapterResult, observation: Observation) -> AdapterResult:
    return base.model_copy(update={"parsed_response": _coerced_safe_response(observation)})


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "LiteLlmAdapter",
    "build_messages",
]
