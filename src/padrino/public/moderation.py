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


#: Softer patterns that are masked-in-place rather than blocked outright: the
#: message is still released but the offending span is replaced with ``*``
#: (the SOFT_MASK verdict). The ``mask_word`` sentinel lets unit tests trigger a
#: known soft-mask without a real slur. These NEVER trip the hard first-pass.
_SOFT_MASK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bdamn\b",
        r"\bhell\b",
        r"\bmask_word\b",  # unit-test sentinel
    ]
)


#: Event types whose payload carries spectator-visible chat text. The engine
#: emits ``PublicMessageSubmitted`` (core/engine/events.py) with the text under
#: ``payload["text"]``; matching anything else would silently skip moderation.
_PUBLIC_CHAT_EVENT_TYPES: tuple[str, ...] = ("PublicMessageSubmitted",)


def _extract_public_messages(events: Sequence[dict[str, Any]]) -> list[str]:
    """Pull text from public chat events in the public_event_v1 envelope."""
    messages: list[str] = []
    for ev in events:
        if ev.get("event_type") in _PUBLIC_CHAT_EVENT_TYPES:
            payload = ev.get("payload", {})
            text = payload.get("text")
            if text:
                messages.append(str(text))
    return messages


def deterministic_first_pass(events: Sequence[dict[str, Any]]) -> bool:
    """Return True iff no banned pattern appears in any public message.

    Pure synchronous function: no I/O, no clock reads, no side-effects.
    """
    return all(deterministic_first_pass_message(msg) for msg in _extract_public_messages(events))


def deterministic_first_pass_message(text: str) -> bool:
    """Return True iff ``text`` contains no banned pattern (pure single message).

    This is the per-message instant backstop the block-before-release human
    moderation gate (US-140) runs inside the buffer hold: it never makes an I/O
    or clock call, so it is the deterministic verdict the hardened fail path
    falls back to when the async guard model times out or errors.
    """
    return all(not pattern.search(text) for pattern in _TOXIC_PATTERNS)


def deterministic_span_mask(text: str) -> tuple[str, bool]:
    """Mask every banned-pattern span in ``text`` with ``*`` (pure, deterministic).

    Returns ``(masked_text, did_mask)``. The mask replaces each matched span with
    a same-length run of ``*`` so the surrounding message is preserved while the
    offending span is removed — the SOFT_MASK verdict's release text. Pure: no
    I/O, no clock, no RNG, so two runs over the same input always agree.
    """
    masked = text
    did_mask = False
    for pattern in _SOFT_MASK_PATTERNS:
        masked, count = pattern.subn(lambda m: "*" * len(m.group(0)), masked)
        if count:
            did_mask = True
    return masked, did_mask


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


def build_guard_from_settings(settings: Any) -> LiteLlmGuardAdapter | None:
    """Construct the production guard adapter from settings, or None.

    Returns None when no DeepInfra key resolves — the gate then fails closed,
    so with continuous matchmaking enabled but no key, NO game ever becomes
    broadcastable. Callers (the scheduler CLI) should warn loudly on None.
    """
    import os

    from padrino.llm.secrets import resolve_secret

    raw = settings.deepinfra_api_key or os.environ.get("DEEPINFRA_API_KEY")
    if not raw:
        return None
    try:
        key = resolve_secret(raw)
    except Exception:
        key = raw
    return LiteLlmGuardAdapter(model=settings.padrino_guard_model, api_key=key)


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
    "build_guard_from_settings",
    "deterministic_first_pass",
    "deterministic_first_pass_message",
    "deterministic_span_mask",
    "is_broadcastable",
]
