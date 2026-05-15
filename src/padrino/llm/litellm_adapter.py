"""LiteLLM-backed :class:`LlmAdapter` with primary/fallback routing.

`LiteLlmAdapter` issues a single :func:`litellm.acompletion` call against the
:attr:`RoutingPolicy.primary_model`. If that call raises (network error, rate
limit, timeout, etc.) and a fallback model is configured, the adapter retries
exactly once against :attr:`RoutingPolicy.fallback_model`. The final
:class:`AdapterResult` returned to the caller carries the synthesized routing
status (``ok``, ``fallback_ok``, ``primary_failed``, ``both_failed``); per-call
attempt records are exposed on :attr:`LiteLlmAdapter.last_attempts` for the
recording layer to mirror as separate ``llm_calls`` rows.

Parse failures (invalid JSON, schema violations) are NOT retried — they are not
provider-side errors, and the runner already coerces them to a safe action via
:func:`padrino.core.agents.coercion.coerce_response_failure`. Only call-site
exceptions trigger fallback.

Impure module: lives in the ``llm`` layer and is never imported by pure-core.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import litellm

from padrino.core.agents.contract import (
    AgentResponse,
    ResponseError,
    parse_agent_response,
)
from padrino.core.engine.actions import Action
from padrino.core.engine.rng import SeededRng
from padrino.core.enums import ActionType, Role
from padrino.core.observations import Observation
from padrino.llm.adapter import (
    AdapterResult,
    AdapterStatus,
    AgentBuild,
    RoutingPolicy,
)
from padrino.llm.retry import (
    LlmCallFailed,
    RetryExhausted,
    RetryPolicy,
    default_retry_policy,
    with_retry,
)
from padrino.llm.secrets import resolve_secret

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


class LiteLlmAdapter:
    """LiteLLM-backed adapter with primary→fallback routing."""

    __slots__ = (
        "_auth_secret",
        "_build",
        "_policy",
        "_retry_policy",
        "_sleeper",
        "_system_prompt",
        "_system_prompts_by_role",
        "_timeout_s",
        "last_attempts",
    )

    def __init__(
        self,
        *,
        routing_policy: RoutingPolicy,
        agent_build: AgentBuild,
        timeout_s: float,
        auth_secret_ref: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        system_prompts_by_role: Mapping[Role, str] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        # Resolve credentials once at construction so a misconfigured provider
        # fails loudly at boot instead of silently 401-ing on first call.
        self._auth_secret = resolve_secret(auth_secret_ref)
        self._policy = routing_policy
        self._build = agent_build
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
        self.last_attempts: tuple[AdapterResult, ...] = ()

    async def complete(self, observation: Observation) -> AdapterResult:
        attempts: list[AdapterResult] = []

        primary = await self._call_model(observation, self._policy.primary_model)
        attempts.append(primary)

        if primary.error is None:
            self.last_attempts = tuple(attempts)
            return primary

        if self._policy.fallback_model is None:
            terminal_status: AdapterStatus = (
                "exhausted" if primary.failure is not None else "primary_failed"
            )
            failed = primary.model_copy(update={"status": terminal_status})
            attempts[-1] = failed
            self.last_attempts = tuple(attempts)
            return _with_coerced_response(failed, observation)

        attempts[-1] = primary.model_copy(update={"status": "primary_failed"})

        fallback = await self._call_model(observation, self._policy.fallback_model)
        if fallback.error is None:
            promoted = fallback.model_copy(update={"status": "fallback_ok"})
            attempts.append(promoted)
            self.last_attempts = tuple(attempts)
            return promoted

        # Either path exhausting retries promotes the final status to
        # ``exhausted`` so ``tick.py`` emits a single ``ActionTimedOut`` with
        # ``reason='llm_exhausted'``. Non-retryable failures keep the legacy
        # ``both_failed`` shape (parsed_response is coerced to a safe action).
        is_exhausted = fallback.failure is not None or primary.failure is not None
        synthesized_status: AdapterStatus = "exhausted" if is_exhausted else "both_failed"
        synthesized = _with_coerced_response(
            fallback.model_copy(update={"status": synthesized_status}),
            observation,
        )
        attempts.append(synthesized)
        self.last_attempts = tuple(attempts)
        return synthesized

    def _system_prompt_for(self, observation: Observation) -> str:
        if self._system_prompts_by_role is None:
            return self._system_prompt
        return self._system_prompts_by_role.get(observation.you.role, self._system_prompt)

    async def _call_model(self, observation: Observation, model_id: str) -> AdapterResult:
        messages = build_messages(observation, system_prompt=self._system_prompt_for(observation))
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "timeout": self._timeout_s,
            **self._build.inference_params,
        }
        start = time.monotonic()
        # Deterministic jitter seed: bind to the observation contents so a
        # replay of the same observation produces an identical backoff schedule.
        rng = SeededRng(f"{observation.phase}:{observation.you.player_id}:{model_id}")

        async def _call() -> Any:
            return await litellm.acompletion(**kwargs)

        try:
            response, _attempt_history = await with_retry(
                _call,
                self._retry_policy,
                sleeper=self._sleeper,
                rng=rng,
            )
        except RetryExhausted as exc:
            latency_ms = _elapsed_ms(start)
            failure = LlmCallFailed(
                error_kind=exc.error_kind,
                error_message=exc.error_message,
                attempts=exc.attempts,
            )
            return AdapterResult(
                raw_response="",
                parsed_response=ResponseError(
                    reason="SCHEMA_VIOLATION",
                    details=f"{exc.error_kind}: {exc.error_message}",
                ),
                latency_ms=latency_ms,
                status="provider_error",
                error=f"{exc.error_kind}: {exc.error_message}",
                failure=failure,
            )
        except Exception as exc:
            latency_ms = _elapsed_ms(start)
            return AdapterResult(
                raw_response="",
                parsed_response=ResponseError(
                    reason="SCHEMA_VIOLATION",
                    details=f"{type(exc).__name__}: {exc}",
                ),
                latency_ms=latency_ms,
                status="provider_error",
                error=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = _elapsed_ms(start)
        text = _extract_text(response)
        parsed = parse_agent_response(text)
        status: AdapterStatus
        if isinstance(parsed, AgentResponse):
            status = "ok"
        elif parsed.reason == "INVALID_JSON":
            status = "invalid_json"
        else:
            status = "schema_violation"

        return AdapterResult(
            raw_response=text,
            parsed_response=parsed,
            latency_ms=latency_ms,
            input_tokens=_extract_input_tokens(response),
            output_tokens=_extract_output_tokens(response),
            cost_usd=_extract_cost(response),
            provider_response_id=_extract_response_id(response),
            status=status,
        )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _extract_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    return ""


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
        cost = hidden.get("response_cost")
        if isinstance(cost, int | float):
            return float(cost)
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
