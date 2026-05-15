"""Bounded exponential backoff with deterministic jitter for LLM adapter calls.

US-053. The :class:`RetryPolicy` value object configures the loop; the
``with_retry`` coroutine drives it. Time and randomness are both injected
(``sleeper`` and ``rng``) so adapter tests pin the retry schedule without
sleeping in real wall-clock and replay produces an identical attempt history.

On exhaustion the helper raises :class:`RetryExhausted` carrying the last
provider error and the number of attempts taken. The adapter translates that
into an :class:`AdapterResult` with ``status='exhausted'`` and a
:class:`LlmCallFailed` summary; ``tick.py`` then emits a single
``ActionTimedOut`` event with ``reason='llm_exhausted'``.

Impure module — lives in the ``llm`` layer and never touches the DB, network,
wall-clock, or stdlib ``random``. Pure-core code does not import it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, TypeVar

import structlog
from litellm.exceptions import (
    APIConnectionError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field

from padrino.core.engine.rng import SeededRng
from padrino.observability.events import EVENT_LLM_CALL_RETRY

T = TypeVar("T")

DEFAULT_RETRY_ON: Final[tuple[type[BaseException], ...]] = (
    RateLimitError,
    APIConnectionError,
    InternalServerError,
    TimeoutError,
)


_logger = structlog.get_logger("padrino.llm")


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """One recorded attempt that failed and was followed by a sleep + retry."""

    attempt_number: int
    delay_ms: int
    error_kind: str
    error_message: str


class LlmCallFailed(BaseModel):
    """Structured dead-letter outcome after every retry was consumed."""

    model_config = ConfigDict(frozen=True)

    error_kind: str
    error_message: str
    attempts: int


class RetryExhausted(Exception):
    """Raised by :func:`with_retry` when ``max_attempts`` is reached."""

    __slots__ = ("attempts", "last_error")

    def __init__(self, *, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"retries exhausted after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error

    @property
    def error_kind(self) -> str:
        return type(self.last_error).__name__

    @property
    def error_message(self) -> str:
        return str(self.last_error)


class RetryPolicy(BaseModel):
    """Bounded exponential backoff parameters.

    ``max_attempts`` includes the first attempt (so ``max_attempts=3`` permits
    one initial call plus two retries). ``retry_on`` is the exception classes
    that trigger a retry; every other exception short-circuits the loop.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    max_attempts: int = Field(gt=0)
    base_delay_s: float = Field(ge=0.0)
    max_delay_s: float = Field(ge=0.0)
    retry_on: tuple[type[BaseException], ...]


def default_retry_policy() -> RetryPolicy:
    """Return the canonical Padrino retry policy.

    Three attempts (one initial + two retries), 0.5s base delay, capped at 16s,
    retrying on rate-limits, connection errors, 5xx, and asyncio timeouts.
    """
    return RetryPolicy(
        max_attempts=3,
        base_delay_s=0.5,
        max_delay_s=16.0,
        retry_on=DEFAULT_RETRY_ON,
    )


def _is_retryable(exc: BaseException, retry_on: tuple[type[BaseException], ...]) -> bool:
    return any(isinstance(exc, klass) for klass in retry_on)


def _compute_delay(*, attempt_idx: int, policy: RetryPolicy, rng: SeededRng) -> float:
    """Return the delay before the (attempt_idx+1)th attempt.

    Exponential growth (``base * 2 ** attempt_idx``) capped at
    ``max_delay_s``, then multiplied by ``[0.5, 1.0]`` decorrelated jitter
    drawn from the supplied :class:`SeededRng`.
    """
    base = policy.base_delay_s * (2**attempt_idx)
    capped = min(base, policy.max_delay_s)
    # Jitter is a uniformly-random fraction in [0.5, 1.0]; the integer draw
    # is 0..1000 so two calls with the same seed produce identical schedules.
    jitter = 0.5 + (rng.randbelow(1001) / 2000.0)
    return float(capped * jitter)


async def with_retry(
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleeper: Callable[[float], Awaitable[None]],
    rng: SeededRng,
) -> tuple[T, list[RetryAttempt]]:
    """Invoke ``call`` with bounded retries; sleep between attempts via ``sleeper``.

    Returns ``(result, attempts)`` where ``attempts`` records every failed
    attempt that preceded the eventual success. Raises :class:`RetryExhausted`
    if ``max_attempts`` is consumed without success; raises the original
    exception unchanged if the failure is not in ``policy.retry_on``.
    """
    attempts: list[RetryAttempt] = []
    last_error: BaseException | None = None
    for idx in range(policy.max_attempts):
        try:
            result = await call()
        except BaseException as exc:
            if not _is_retryable(exc, policy.retry_on):
                raise
            last_error = exc
            attempt_number = idx + 1
            if attempt_number >= policy.max_attempts:
                raise RetryExhausted(attempts=attempt_number, last_error=exc) from exc
            delay_s = _compute_delay(attempt_idx=idx, policy=policy, rng=rng)
            delay_ms = int(delay_s * 1000)
            attempts.append(
                RetryAttempt(
                    attempt_number=attempt_number,
                    delay_ms=delay_ms,
                    error_kind=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            _logger.info(
                EVENT_LLM_CALL_RETRY,
                attempt_number=attempt_number,
                delay_ms=delay_ms,
                error_kind=type(exc).__name__,
            )
            await sleeper(delay_s)
            continue
        return result, attempts
    # The loop body either returns or raises; this is unreachable but keeps
    # mypy honest about the function's nominal return type.
    assert last_error is not None
    raise RetryExhausted(attempts=policy.max_attempts, last_error=last_error)


__all__ = [
    "DEFAULT_RETRY_ON",
    "LlmCallFailed",
    "RetryAttempt",
    "RetryExhausted",
    "RetryPolicy",
    "default_retry_policy",
    "with_retry",
]
