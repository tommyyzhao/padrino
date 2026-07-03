"""US-300: ranked-ban repository behavior."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.ranked_ban import RankedBanKind
from padrino.db.models import RankedBan
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.repositories import oauth_identities, ranked_bans


async def _account_with_email(
    session: AsyncSession,
    *,
    subject: str,
    email: str | None,
    verified: bool = True,
) -> str:
    principal = await oauth_identities.find_or_create_account(
        session,
        provider="test-oauth",
        subject=subject,
        display_name=f"Account {subject}",
        email=email,
        email_verified=verified,
        now=datetime(2026, 6, 27, tzinfo=UTC),
    )
    return str(principal.id)


async def test_verified_email_ban_hash_inherits_to_new_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        original_id = await _account_with_email(
            session,
            subject="first",
            email="  Person@Example.COM  ",
        )
        issued = await ranked_bans.issue_ban_for_principal(
            session,
            principal_id=original_id,
            kind=RankedBanKind.BAN,
            reason="manual_review",
            created_by="admin-key",
            now=now,
        )
        new_id = await _account_with_email(
            session,
            subject="second",
            email="person@example.com",
        )

    expected_hash = hashlib.sha256(b"person@example.com").hexdigest()
    assert issued.email_hash == expected_hash
    assert issued.email_hash != "person@example.com"

    async with session_factory() as session:
        assert await ranked_bans.is_principal_ranked_banned(
            session, principal_id=original_id, now=now
        )
        assert await ranked_bans.is_principal_ranked_banned(session, principal_id=new_id, now=now)
        rows = list((await session.execute(select(RankedBan))).scalars())

    assert len(rows) == 1
    serialized = repr(rows[0].__dict__)
    assert "Person@Example.COM" not in serialized
    assert "person@example.com" not in serialized


async def test_principal_without_verified_email_gets_principal_only_ban(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(
            session,
            kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
            display_name="No Email",
        )
        issued = await ranked_bans.issue_ban_for_principal(
            session,
            principal_id=principal.id,
            kind=RankedBanKind.BAN,
            reason="manual_review",
            created_by="admin-key",
            now=now,
        )
        principal_id = principal.id

    assert issued.email_hash is None

    async with session_factory() as session:
        assert await ranked_bans.is_principal_ranked_banned(
            session, principal_id=principal_id, now=now
        )
