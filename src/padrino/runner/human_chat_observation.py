"""Resolve released human-chat sidecar text for outbound observations.

Hash-chained human chat events carry only ``content_ref`` and an empty ``text``
field. This impure runner helper resolves the approved sidecar ``cleaned_text``
by event sequence right before an observation leaves the server for an AI or a
human client. The raw text is never read here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.observations import EventEntry, Observation
from padrino.db.repositories import human_chat as sidecar_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter


async def load_released_human_chat_texts(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> dict[int, str]:
    """Return sidecar approved text keyed by paired ``game_events.sequence``."""
    rows = await sidecar_repo.list_for_game(session, game_id)
    return {
        row.sequence: row.cleaned_text
        for row in rows
        if row.cleaned_text is not None and not row.redacted
    }


def _hydrate_entry(entry: EventEntry, texts_by_sequence: Mapping[int, str]) -> EventEntry:
    if entry.event_type not in {"PublicMessageSubmitted", "PrivateMessageSubmitted"}:
        return entry
    text = texts_by_sequence.get(entry.sequence)
    if text is None:
        return entry
    if not entry.payload.get("content_ref"):
        return entry
    if entry.payload.get("text") not in {"", None}:
        return entry
    return entry.model_copy(update={"payload": {**entry.payload, "text": text}})


def hydrate_observation_human_chat(
    observation: Observation,
    texts_by_sequence: Mapping[int, str],
) -> Observation:
    """Fill ref-only chat events with sidecar approved text for outbound use."""
    if not texts_by_sequence:
        return observation
    public_events = tuple(
        _hydrate_entry(entry, texts_by_sequence) for entry in observation.public_events
    )
    private_events = tuple(
        _hydrate_entry(entry, texts_by_sequence) for entry in observation.private_events
    )
    return observation.model_copy(
        update={"public_events": public_events, "private_events": private_events}
    )


class HumanChatHydratingAdapter:
    """Adapter wrapper that resolves released human chat before AI completion."""

    __slots__ = ("_game_id", "_inner", "_session_factory")

    def __init__(
        self,
        *,
        inner: LlmAdapter,
        session_factory: async_sessionmaker[AsyncSession],
        game_id: uuid.UUID,
    ) -> None:
        self._inner = inner
        self._session_factory = session_factory
        self._game_id = game_id

    async def complete(self, observation: Observation) -> AdapterResult:
        async with self._session_factory() as session:
            texts_by_sequence = await load_released_human_chat_texts(
                session,
                game_id=self._game_id,
            )
        hydrated = hydrate_observation_human_chat(observation, texts_by_sequence)
        return await self._inner.complete(hydrated)


__all__ = [
    "HumanChatHydratingAdapter",
    "hydrate_observation_human_chat",
    "load_released_human_chat_texts",
]
