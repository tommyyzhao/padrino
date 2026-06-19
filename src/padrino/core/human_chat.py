"""Pure helper for binding out-of-band human chat to the hash-chained log (US-123).

Human free-text chat is personally-identifiable and must be erasable for GDPR.
Storing the raw text inside a hashed event would make erasure mathematically
impossible without breaking deterministic replay (any change to the payload
changes the ``event_hash`` and snaps the chain). So the paired
``PublicMessageSubmitted`` / ``PrivateMessageSubmitted`` core event for a human
seat carries ONLY a content reference (a SHA-256 of the message) instead of the
raw text; the raw/cleaned text lives in the ``human_chat_messages`` sidecar.

``content_ref`` is a deterministic function of the message text, so it is safe
to live inside the hashed event (it is pure data, NOT wall-clock or random) and
redacting the sidecar never has to touch the event row. The chat-firewall is
preserved: the reference is opaque and drives no game mechanics.
"""

from __future__ import annotations

import hashlib
from typing import Final

_CONTENT_REF_PREFIX: Final[str] = "sha256:"


def human_chat_content_ref(text: str) -> str:
    """Return the opaque content reference for a human chat message.

    The reference is ``"sha256:" + hex(sha256(utf-8 text))``. It is the only
    representation of human chat that may appear in a hash-chained event payload;
    the raw text itself lives exclusively in the ``human_chat_messages`` sidecar
    so it can be redacted without perturbing any ``event_hash``.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_CONTENT_REF_PREFIX}{digest}"


__all__ = ["human_chat_content_ref"]
