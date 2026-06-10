"""Moderation gate: is_broadcastable fail-closed (US-093).

Two-layer content gate over public_event_v1 message payloads:

  1. Deterministic first-pass — regex / keyword filters, pure and synchronous.
     If any public message hits a banned pattern the game is immediately
     non-broadcastable (no guard-model call is made).

  2. GuardModelAdapter pass — async call to an LLM guard model (default: a
     DeepInfra-hosted Llama-Guard-family model via the existing LiteLLM adapter
     and ``DEEPINFRA_API_KEY``).  Any exception from the adapter causes the
     gate to FAIL-CLOSED (non-broadcastable).

``is_broadcastable(events, guard)`` is the public entry point.  Passing
``guard=None`` means the guard pass is unavailable — the gate also fails closed.

Real LLM calls are behind ``@pytest.mark.integration``; the deterministic path
and fail-closed behaviour are covered by default unit tests.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Deterministic first-pass filter
# ---------------------------------------------------------------------------

# Permissive substring, case-insensitive patterns.  The guard model provides
# nuanced second opinion.  The ``toxic_word`` sentinel is intentionally
# included so unit tests can seed a known-failing message without real slurs.
_TOXIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bkill yourself\b",
        r"\bkys\b",
        r"\bfaggot\b",
        r"n[i1][g6]+[ae3]r",
        r"\btoxic_word\b",  # unit-test sentinel
    ]
)


def _extract_public_messages(events: Sequence[dict[str, Any]]) -> list[str]:
    """Pull text from public chat events in the public_event_v1 envelope."""
    messages: list[str] = []
    for ev in events:
        if ev.get("event_type") in ("PublicMessage", "ChatMessage"):
            payload = ev.get("payload", {})
            text = payload.get("text") or payload.get("message") or payload.get("content")
            if text:
                messages.append(str(text))
    return messages


def deterministic_first_pass(events: Sequence[dict[str, Any]]) -> bool:
    """Return True iff no banned pattern appears in any public message.

    Pure synchronous function: no I/O, no clock reads, no side-effects.
    """
    for msg in _extract_public_messages(events):
        for pattern in _TOXIC_PATTERNS:
            if pattern.search(msg):
                return False
    return True


# ---------------------------------------------------------------------------
# GuardModelAdapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GuardModelAdapter(Protocol):
    """Pluggable async interface for an LLM-based content guard pass."""

    async def check(self, messages: list[str]) -> bool:
        """Return True if the supplied messages are safe to broadcast.

        Any exception propagates to the caller; ``is_broadcastable`` catches
        it and returns ``False`` (fail-closed).
        """
        ...


# ---------------------------------------------------------------------------
# Real LiteLLM-backed guard adapter (integration use only)
# ---------------------------------------------------------------------------


class LiteLlmGuardAdapter:
    """Llama-Guard family model via LiteLLM (real network call).

    Instantiate with the model ID from settings and the resolved API key.
    Llama-Guard models respond with a first token of ``safe`` or ``unsafe``.
    """

    __slots__ = ("_api_key", "_model", "_timeout_s")

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def check(self, messages: list[str]) -> bool:
        import litellm  # local import: this adapter lives in the impure layer

        user_content = "\n\n".join(messages) if messages else "(no messages)"
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": user_content}],
            "timeout": self._timeout_s,
            "max_tokens": 10,
        }
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key

        response = await litellm.acompletion(**kwargs)
        text: str = (response.choices[0].message.content or "").strip().lower()
        return text.startswith("safe")


# ---------------------------------------------------------------------------
# Top-level gate
# ---------------------------------------------------------------------------


async def is_broadcastable(
    events: Sequence[dict[str, Any]],
    guard: GuardModelAdapter | None,
) -> bool:
    """Return True iff the game transcript is safe to broadcast publicly.

    Fail-closed:
    - ``guard=None``  → unavailable → ``False``
    - ``guard.check`` raises → error → ``False``
    - deterministic first-pass fails → ``False`` (guard not called)
    """
    if not deterministic_first_pass(events):
        return False

    if guard is None:
        return False

    messages = _extract_public_messages(events)
    try:
        return await guard.check(messages)
    except Exception:
        return False


__all__ = [
    "GuardModelAdapter",
    "LiteLlmGuardAdapter",
    "deterministic_first_pass",
    "is_broadcastable",
]
