"""Block-before-release moderation hook seam for human chat (US-135).

US-135 defines the chat *channel* + the buffer *hold* + sidecar routing and a
**stub-pass** gate; the real moderation verdict (deterministic first-pass + async
guard model, hardened fail path) lands in US-140. This module isolates that seam
so US-140 can swap the verdict in without touching the channel.

A :class:`ChatModerationHook` takes one held message and returns a
:class:`ChatModerationVerdict`. :class:`StubPassModerationHook` is the v1
default — it ALLOWs every message (US-140 replaces it with the real gate). The
hook is async so US-140's guard-model call drops in without changing the channel
contract; the stub awaits nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ChatVerdict(StrEnum):
    """Block-before-release verdict for a single human chat message (US-140)."""

    ALLOW = "ALLOW"
    SOFT_MASK = "SOFT_MASK"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class ChatModerationVerdict:
    """The moderation outcome for one held message.

    ``cleaned_text`` is the text to release (identical to the input for ALLOW;
    span-masked for SOFT_MASK). A BLOCK carries ``cleaned_text=None`` — a blocked
    message is never released and never chained.
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


class StubPassModerationHook:
    """v1 stub gate: ALLOW every message unchanged (US-140 lands the real verdict)."""

    async def review(
        self, *, public_player_id: str, channel: str, text: str
    ) -> ChatModerationVerdict:
        return ChatModerationVerdict(verdict=ChatVerdict.ALLOW, cleaned_text=text)


__all__ = [
    "ChatModerationHook",
    "ChatModerationVerdict",
    "ChatVerdict",
    "StubPassModerationHook",
]
