"""Bounded exponential backoff with deterministic jitter for LLM adapter calls.

US-053. The :class:`RetryPolicy` value object configures the loop; the
``with_retry`` coroutine drives it. Time and randomness are both injected
(``sleeper`` and ``rng``) so adapter tests pin the retry schedule without
sleeping in real wall-clock and replay produces an identical attempt history.

On exhaustion the helper raises :class:`RetryExhausted` carrying the last
provider error and the number of attempts taken. The adapter translates that
into an :class:`AdapterResult` carrying a :class:`LlmCallFailed` summary and a
``failure_reason``; ``tick.py`` then emits a single ``ActionTimedOut`` event.

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
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from pydantic import BaseModel, ConfigDict, Field

from padrino.core.engine.rng import SeededRng
from padrino.observability.events import EVENT_LLM_CALL_RETRY

T = TypeVar("T")

DEFAULT_RETRY_ON: Final[tuple[type[BaseException], ...]] = (
    RateLimitError,
    APIConnectionError,
    InternalServerError,
    ServiceUnavailableError,
    BadGatewayError,
    Timeout,
    TimeoutError,
)


# Overload-type errors back off from a longer base: a gateway that answers 503 in a few
# milliseconds would otherwise burn every attempt inside one second.
OVERLOAD_ERRORS: Final[tuple[type[BaseException], ...]] = (
    RateLimitError,
    ServiceUnavailableError,
    BadGatewayError,
)
DEFAULT_OVERLOAD_BASE_DELAY_S: Final[float] = 2.0

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
    # Backoff base for ``OVERLOAD_ERRORS``; 0 means "use ``base_delay_s``".
    overload_base_delay_s: float = Field(default=0.0, ge=0.0)


def default_retry_policy() -> RetryPolicy:
    """Return the canonical Padrino retry policy.

    Three attempts (one initial + two retries), 0.5s base delay (2s for overload errors:
    503/502/429), capped at 16s,
    retrying on rate-limits, connection errors, 5xx/502/503, and provider or asyncio
    timeouts. 4xx client errors (auth, not-found, bad request) are never retried.
    """
    return RetryPolicy(
        max_attempts=3,
        base_delay_s=0.5,
        max_delay_s=16.0,
        retry_on=DEFAULT_RETRY_ON,
        overload_base_delay_s=DEFAULT_OVERLOAD_BASE_DELAY_S,
    )


def _is_retryable(exc: BaseException, retry_on: tuple[type[BaseException], ...]) -> bool:
    return any(isinstance(exc, klass) for klass in retry_on)


def retry_after_s(exc: BaseException) -> float | None:
    """Seconds from a numeric ``Retry-After`` header on the error's HTTP response, if any."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None
    return value if value >= 0.0 else None


def _compute_delay(
    *, attempt_idx: int, policy: RetryPolicy, rng: SeededRng, exc: BaseException | None = None
) -> float:
    """Return the delay before the (attempt_idx+1)th attempt.

    Exponential growth (``base * 2 ** attempt_idx``) capped at ``max_delay_s``, then
    multiplied by ``[0.5, 1.0]`` decorrelated jitter drawn from the supplied
    :class:`SeededRng`. Overload errors use ``overload_base_delay_s`` as the base, and a
    numeric ``Retry-After`` header (itself capped at ``max_delay_s``) is a lower bound.
    """
    base_delay = policy.base_delay_s
    if exc is not None and policy.overload_base_delay_s > 0 and isinstance(exc, OVERLOAD_ERRORS):
        base_delay = max(base_delay, policy.overload_base_delay_s)
    base = base_delay * (2**attempt_idx)
    capped = min(base, policy.max_delay_s)
    # Jitter is a uniformly-random fraction in [0.5, 1.0]; the integer draw
    # is 0..1000 so two calls with the same seed produce identical schedules.
    jitter = 0.5 + (rng.randbelow(1001) / 2000.0)
    delay = float(capped * jitter)
    hinted = retry_after_s(exc) if exc is not None else None
    if hinted is not None:
        delay = max(delay, min(hinted, policy.max_delay_s))
    return delay


async def with_retry(
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleeper: Callable[[float], Awaitable[None]],
    rng: SeededRng,
    remaining_s: Callable[[], float] | None = None,
    min_remaining_s: float = 0.0,
) -> tuple[T, list[RetryAttempt]]:
    """Invoke ``call`` with bounded retries; sleep between attempts via ``sleeper``.

    Returns ``(result, attempts)`` where ``attempts`` records every failed
    attempt that preceded the eventual success. Raises :class:`RetryExhausted`
    if ``max_attempts`` is consumed without success; raises the original
    exception unchanged if the failure is not in ``policy.retry_on``.

    ``remaining_s`` (seconds left in the caller's deadline) makes the loop deadline-aware:
    a sleep is clamped so at least ``min_remaining_s`` is still left afterwards, and when
    that is impossible the loop gives up now (:class:`RetryExhausted`) instead of sleeping
    past the deadline or starting a billed attempt that cannot finish.
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
            delay_s = _compute_delay(attempt_idx=idx, policy=policy, rng=rng, exc=exc)
            if remaining_s is not None:
                delay_s = min(delay_s, remaining_s() - min_remaining_s)
                if delay_s < 0.0:
                    raise RetryExhausted(attempts=attempt_number, last_error=exc) from exc
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
    "DEFAULT_OVERLOAD_BASE_DELAY_S",
    "DEFAULT_RETRY_ON",
    "OVERLOAD_ERRORS",
    "LlmCallFailed",
    "RetryAttempt",
    "RetryExhausted",
    "RetryPolicy",
    "default_retry_policy",
    "retry_after_s",
    "with_retry",
]
