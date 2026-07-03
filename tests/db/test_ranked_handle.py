"""Ranked handle repository tests (US-297)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.repositories import human_principals
from padrino.db.repositories.human_principals import (
    RankedHandleCollisionError,
    claim_ranked_handle,
)

NOW = datetime(2026, 6, 27, tzinfo=UTC)


async def test_claim_ranked_handle_collides_case_insensitively_with_409(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        first = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name="First Raw Name",
        )
        second = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name="Second Raw Name",
        )
        first_id = first.id
        second_id = second.id
        await claim_ranked_handle(
            session,
            first_id,
            candidate="RankedHero",
            blocklist=(),
            reserved=(),
            now=NOW,
        )

    async with session_factory() as session:
        with pytest.raises(RankedHandleCollisionError) as exc_info:
            async with session.begin():
                await claim_ranked_handle(
                    session,
                    second_id,
                    candidate="rankedhero",
                    blocklist=(),
                    reserved=(),
                    now=NOW,
                )

    assert exc_info.value.status_code == 409

    async with session_factory() as session:
        loaded_first = await human_principals.get_principal(session, first_id)
        loaded_second = await human_principals.get_principal(session, second_id)
        assert loaded_first is not None
        assert loaded_first.ranked_handle == "RankedHero"
        assert loaded_first.ranked_handle_normalized == "rankedhero"
        assert loaded_second is not None
        assert loaded_second.ranked_handle is None
        assert loaded_second.ranked_handle_normalized is None


async def test_default_ranked_handle_is_deterministic_stable_and_pseudonymous(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name="Alice Raw Display",
        )
        principal_id = principal.id

        first = await claim_ranked_handle(
            session,
            principal_id,
            candidate=None,
            blocklist=(),
            reserved=(),
            now=NOW,
        )
        assert first is not None
        first_handle = first.ranked_handle
        second = await claim_ranked_handle(
            session,
            principal_id,
            candidate=None,
            blocklist=(),
            reserved=(),
            now=NOW,
        )
        assert second is not None

    assert first_handle == second.ranked_handle
    assert first_handle is not None
    assert first_handle.startswith("Player-")
    assert "Alice" not in first_handle
    assert "Raw" not in first_handle
    assert str(principal_id) not in first_handle
    assert principal_id.hex not in first_handle
    assert second.ranked_handle_normalized == first_handle.casefold()
