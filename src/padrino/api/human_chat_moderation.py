"""Block-before-release moderation for human chat (US-135 seam, US-140 verdict).

US-135 defined the chat *channel* + buffer *hold* + sidecar routing and a
**stub-pass** gate. US-140 lands the real verdict: a real-time gate that runs
INSIDE the buffer hold window before any other seat sees the message.

The gate combines:

  1. a pure deterministic first-pass (``public.moderation`` single-message
     verdict + span-mask) — the instant backstop, never an I/O / clock call; and
  2. an async guard model (:class:`MessageGuardAdapter`) under a HARD latency
     budget.

HARDENED fail path (the reviewers' flagged risk): the deterministic first-pass
is the instant backstop. On guard timeout/error the gate falls back to the
deterministic-only verdict for THAT message — the game NEVER halts. A BLOCK is
never released and never chained; release timing is the symmetric delay handled
by the channel/tick, independent of which moderation path produced the verdict
(no timing leak). The pure sanitizer (``core/agents/sanitizer.py``) is wired to
the live human text so cosmetic encoding tricks are normalized before release.

A :class:`ChatModerationHook` takes one held message and returns a
:class:`ChatModerationVerdict` (ALLOW / SOFT_MASK / BLOCK). The hook is async so
the guard-model call fits the channel contract unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from padrino.core.agents.sanitizer import sanitize_visible_text
from padrino.public.moderation import (
    deterministic_first_pass_message,
    deterministic_span_mask,
)

#: Upper bound on text the sanitizer keeps; the per-channel ruleset limit is
#: already enforced in the channel before the hook runs, so this is only a
#: defensive ceiling so a pathological message cannot inflate the masked output.
_SANITIZE_MAX_CHARS = 4000

#: Default hard latency budget for the async guard model. Latency hides inside
#: the existing buffer hold window; on timeout the deterministic verdict stands.
_DEFAULT_GUARD_TIMEOUT_S = 2.0


class ChatVerdict(StrEnum):
    """Block-before-release verdict for a single human chat message (US-140)."""

    ALLOW = "ALLOW"
    SOFT_MASK = "SOFT_MASK"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class ChatModerationVerdict:
    """The moderation outcome for one held message.

    ``cleaned_text`` is the text to release (sanitized for ALLOW; span-masked for
    SOFT_MASK). A BLOCK carries ``cleaned_text=None`` — a blocked message is never
    released and never chained.
    """

    verdict: ChatVerdict
    cleaned_text: str | None


@runtime_checkable
class ChatModerationHook(Protocol):
    """Block-before-release gate run inside the buffer hold window (US-140)."""

    async def review(
        self, *, public_player_id: str, channel: str, text: str
    ) -> ChatModerationVerdict:
        """Return the verdict for one held human message."""
        ...


@runtime_checkable
class MessageGuardAdapter(Protocol):
    """Async single-message content guard used inside :class:`RealtimeModerationHook`.

    ``check_message`` returns True iff the (already sanitized) message is safe to
    release. Any exception or a timeout is treated by the hook as the hardened
    fail path: the deterministic-only verdict stands and the game proceeds.
    """

    async def check_message(self, text: str) -> bool:
        """Return True if the message is safe to release."""
        ...


class StubPassModerationHook:
    """v1 stub gate: ALLOW every message unchanged.

    Retained for callers that have not yet wired the real gate; US-140's real
    verdict lives in :class:`RealtimeModerationHook`.
    """

    async def review(
        self, *, public_player_id: str, channel: str, text: str
    ) -> ChatModerationVerdict:
        return ChatModerationVerdict(verdict=ChatVerdict.ALLOW, cleaned_text=text)


class RealtimeModerationHook:
    """The real block-before-release gate (US-140).

    Verdict order for one held message:

    1. Pure deterministic first-pass: a banned-pattern hit is an instant BLOCK —
       the async guard is NOT called and the message is never released/chained.
    2. The pure sanitizer normalizes cosmetic encoding tricks; a pure span-mask
       removes any soft-mask span. If the span-mask altered the message the
       verdict is SOFT_MASK (mask applied before any guard call).
    3. The async guard model runs under a hard latency budget. ``False`` → BLOCK.
       A timeout or any exception falls back to the deterministic verdict for THAT
       message (ALLOW the sanitized/masked text) so the game never halts.

    ``guard=None`` means no guard is configured: the deterministic verdict stands
    on its own (the gate still BLOCKs hard hits and SOFT_MASKs soft hits).
    """

    __slots__ = ("_guard", "_timeout_s")

    def __init__(
        self,
        *,
        guard: MessageGuardAdapter | None = None,
        timeout_s: float = _DEFAULT_GUARD_TIMEOUT_S,
    ) -> None:
        self._guard = guard
        self._timeout_s = timeout_s

    async def review(
        self, *, public_player_id: str, channel: str, text: str
    ) -> ChatModerationVerdict:
        # 1. Instant deterministic backstop. A hard hit is BLOCK; never call the
        #    guard, never release, never chain.
        if not deterministic_first_pass_message(text):
            return ChatModerationVerdict(verdict=ChatVerdict.BLOCK, cleaned_text=None)

        # 2. Pure sanitizer + span-mask produce the candidate release text.
        sanitized = sanitize_visible_text(text, _SANITIZE_MAX_CHARS).cleaned
        masked, did_mask = deterministic_span_mask(sanitized)
        deterministic_verdict = ChatVerdict.SOFT_MASK if did_mask else ChatVerdict.ALLOW

        # 3. Async guard model under a hard latency budget. The deterministic
        #    verdict from step 2 is the hardened fallback.
        if self._guard is None:
            return ChatModerationVerdict(verdict=deterministic_verdict, cleaned_text=masked)

        try:
            safe = await asyncio.wait_for(
                self._guard.check_message(masked), timeout=self._timeout_s
            )
        except Exception:
            # Hardened fail path: any guard timeout (asyncio raises TimeoutError,
            # a subclass of Exception) or error falls back to the deterministic
            # verdict for THIS message so the game never halts.
            return ChatModerationVerdict(verdict=deterministic_verdict, cleaned_text=masked)

        if not safe:
            return ChatModerationVerdict(verdict=ChatVerdict.BLOCK, cleaned_text=None)
        return ChatModerationVerdict(verdict=deterministic_verdict, cleaned_text=masked)


class LiteLlmMessageGuardAdapter:
    """Production single-message guard: a Llama-Guard-family model via LiteLLM.

    This is the concrete :class:`MessageGuardAdapter` the production chat route
    wires into :class:`RealtimeModerationHook` so the async guard arm actually
    runs in production (not only under test stubs). It mirrors
    ``public.moderation.LiteLlmGuardAdapter`` but for the single-message,
    block-before-release human-chat path: ``check_message`` returns True iff the
    (already sanitized/masked) candidate is safe to release.

    The adapter never swallows its own errors — a network failure or timeout
    propagates so :class:`RealtimeModerationHook` takes the hardened fail path
    (the deterministic verdict stands and the game proceeds).
    """

    __slots__ = ("_api_key", "_model", "_timeout_s")

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        timeout_s: float = _DEFAULT_GUARD_TIMEOUT_S,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def check_message(self, text: str) -> bool:
        import litellm  # local import: this adapter lives in the impure layer

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": text}],
            "timeout": self._timeout_s,
            "max_tokens": 10,
        }
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key

        response = await litellm.acompletion(**kwargs)
        verdict: str = (response.choices[0].message.content or "").strip().lower()
        # Llama-Guard models answer with a first token of ``safe`` / ``unsafe``.
        return verdict.startswith("safe")


def build_message_guard_from_settings(settings: Any) -> LiteLlmMessageGuardAdapter | None:
    """Construct the production single-message guard from settings, or None.

    Returns None when no DeepInfra key resolves — the hook then runs the
    deterministic-only arm (it still BLOCKs hard hits and SOFT_MASKs soft hits),
    so a missing guard key degrades to the instant backstop rather than halting
    the game. Mirrors ``public.moderation.build_guard_from_settings``.
    """
    import os

    from padrino.llm.secrets import resolve_secret

    raw = getattr(settings, "deepinfra_api_key", None) or os.environ.get("DEEPINFRA_API_KEY")
    if not raw:
        return None
    try:
        key = resolve_secret(raw)
    except Exception:
        key = raw
    return LiteLlmMessageGuardAdapter(
        model=settings.padrino_guard_model,
        api_key=key,
        timeout_s=settings.padrino_human_chat_guard_timeout_seconds,
    )


__all__ = [
    "ChatModerationHook",
    "ChatModerationVerdict",
    "ChatVerdict",
    "LiteLlmMessageGuardAdapter",
    "MessageGuardAdapter",
    "RealtimeModerationHook",
    "StubPassModerationHook",
    "build_message_guard_from_settings",
]
