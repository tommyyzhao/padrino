"""Tests for applying the human-PII retention plan (US-308)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    Game,
    HumanChatMessage,
    HumanConsent,
    HumanRating,
    HumanSession,
    League,
    Principal,
)
from padrino.db.retention_executor import run_retention_executor
from padrino.settings import Settings

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _settings(*, dry_run: bool) -> Settings:
    return Settings(
        _env_file=None,
        padrino_enable_retention=True,
        padrino_retention_dry_run=dry_run,
        padrino_raw_payload_ttl_days=30,
        padrino_non_broadcastable_game_ttl_days=7,
        padrino_human_chat_retention_ttl_days=30,
        padrino_human_consent_ip_hash_ttl_days=14,
        padrino_abandoned_guest_ttl_days=7,
    )


async def _seed_game(session: AsyncSession) -> uuid.UUID:
    game = Game(
        ruleset_id="mini7_v1",
        game_seed="human-pii-retention",
        status="COMPLETED",
        completed_at=_NOW,
        is_broadcastable=True,
    )
    session.add(game)
    await session.flush()
    return game.id


async def _seed_principal(
    session: AsyncSession,
    *,
    kind: str,
    display_name: str,
    idle_age: timedelta,
) -> uuid.UUID:
    idle_at = _NOW - idle_age
    principal = Principal(
        kind=kind,
        display_name=display_name,
        created_at=idle_at,
        updated_at=idle_at,
    )
    session.add(principal)
    await session.flush()
    session.add(
        HumanSession(
            principal_id=principal.id,
            session_hash=f"session-{principal.id}",
            kind=kind,
            issued_at=idle_at,
            expires_at=idle_at + timedelta(hours=1),
            last_seen_at=idle_at,
        )
    )
    await session.flush()
    return principal.id


async def _seed_human_rating(session: AsyncSession, principal_id: uuid.UUID) -> None:
    league = League(
        name="Humans Included Ranked",
        ruleset_id="mini7_v1",
        ranked=True,
        kind="HUMANS_INCLUDED",
    )
    session.add(league)
    await session.flush()
    session.add(
        HumanRating(
            league_id=league.id,
            human_player_id=str(principal_id),
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            mu=31.0,
            sigma=4.0,
            conservative_score=19.0,
            games=3,
            last_game_at=_NOW - timedelta(days=8),
            updated_at=_NOW - timedelta(days=8),
        )
    )
    await session.flush()


async def _seed_rows(session: AsyncSession) -> dict[str, uuid.UUID]:
    game_id = await _seed_game(session)
    old_guest_id = await _seed_principal(
        session,
        kind="guest",
        display_name="old guest",
        idle_age=timedelta(days=7, seconds=1),
    )
    boundary_guest_id = await _seed_principal(
        session,
        kind="guest",
        display_name="boundary guest",
        idle_age=timedelta(days=7),
    )
    fresh_guest_id = await _seed_principal(
        session,
        kind="guest",
        display_name="fresh guest",
        idle_age=timedelta(days=6, hours=23),
    )
    account_id = await _seed_principal(
        session,
        kind="account",
        display_name="old account",
        idle_age=timedelta(days=45),
    )
    await _seed_human_rating(session, old_guest_id)
    session.add_all(
        [
            HumanChatMessage(
                game_id=game_id,
                sequence=1,
                public_player_id="P01",
                raw_text="old raw",
                cleaned_text="old clean",
                redacted=False,
                created_at=_NOW - timedelta(days=30, seconds=1),
            ),
            HumanChatMessage(
                game_id=game_id,
                sequence=2,
                public_player_id="P01",
                raw_text="boundary raw",
                cleaned_text="boundary clean",
                redacted=False,
                created_at=_NOW - timedelta(days=30),
            ),
            HumanChatMessage(
                game_id=game_id,
                sequence=3,
                public_player_id="P01",
                raw_text="fresh raw",
                cleaned_text="fresh clean",
                redacted=False,
                created_at=_NOW - timedelta(days=29, hours=23),
            ),
            HumanConsent(
                subject_principal_id=old_guest_id,
                document_kind="TOS",
                document_version="v1",
                content_hash="tos",
                accepted_at=_NOW - timedelta(days=14, seconds=1),
                source_ip_hash="old-ip-hash",
            ),
            HumanConsent(
                subject_principal_id=boundary_guest_id,
                document_kind="TOS",
                document_version="v1",
                content_hash="tos",
                accepted_at=_NOW - timedelta(days=14),
                source_ip_hash="boundary-ip-hash",
            ),
            HumanConsent(
                subject_principal_id=fresh_guest_id,
                document_kind="TOS",
                document_version="v1",
                content_hash="tos",
                accepted_at=_NOW - timedelta(days=13, hours=23),
                source_ip_hash="fresh-ip-hash",
            ),
        ]
    )
    await session.flush()
    return {
        "old_guest_id": old_guest_id,
        "boundary_guest_id": boundary_guest_id,
        "fresh_guest_id": fresh_guest_id,
        "account_id": account_id,
    }


async def _rating_snapshot(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        select(
            HumanRating.id,
            HumanRating.league_id,
            HumanRating.human_player_id,
            HumanRating.scope_type,
            HumanRating.scope_value,
            HumanRating.mu,
            HumanRating.sigma,
            HumanRating.conservative_score,
            HumanRating.games,
            HumanRating.last_game_at,
            HumanRating.updated_at,
            HumanRating.quarantined_at,
        ).order_by(HumanRating.id)
    )
    return [tuple(row) for row in rows.all()]


async def test_human_pii_retention_dry_run_mutates_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        ids = await _seed_rows(session)
        before_rating = await _rating_snapshot(session)

    result = await run_retention_executor(
        session_factory,
        settings=_settings(dry_run=True),
        now=_NOW,
    )

    assert result is not None
    assert result.dry_run is True
    assert result.human_chat_messages_redacted == 0
    assert result.human_consent_ip_hashes_nulled == 0
    assert result.guest_principals_anonymized == 0

    async with session_factory() as session:
        old_chat = (
            await session.execute(select(HumanChatMessage).where(HumanChatMessage.sequence == 1))
        ).scalar_one()
        old_consent = (
            await session.execute(
                select(HumanConsent).where(HumanConsent.subject_principal_id == ids["old_guest_id"])
            )
        ).scalar_one()
        old_guest = await session.get(Principal, ids["old_guest_id"])

        assert old_chat.raw_text == "old raw"
        assert old_chat.cleaned_text == "old clean"
        assert old_chat.redacted is False
        assert old_consent.source_ip_hash == "old-ip-hash"
        assert old_guest is not None
        assert old_guest.deleted_at is None
        assert old_guest.display_name == "old guest"
        assert await _rating_snapshot(session) == before_rating


async def test_human_pii_retention_applies_only_when_not_dry_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        ids = await _seed_rows(session)
        before_rating = await _rating_snapshot(session)

    result = await run_retention_executor(
        session_factory,
        settings=_settings(dry_run=False),
        now=_NOW,
    )

    assert result is not None
    assert result.dry_run is False
    assert result.human_chat_messages_redacted == 1
    assert result.human_consent_ip_hashes_nulled == 1
    assert result.guest_principals_anonymized == 1

    async with session_factory() as session:
        chat_rows = {
            row.sequence: row
            for row in (
                await session.execute(select(HumanChatMessage).order_by(HumanChatMessage.sequence))
            ).scalars()
        }
        assert chat_rows[1].raw_text is None
        assert chat_rows[1].cleaned_text is None
        assert chat_rows[1].redacted is True
        assert chat_rows[2].raw_text == "boundary raw"
        assert chat_rows[2].redacted is False
        assert chat_rows[3].raw_text == "fresh raw"
        assert chat_rows[3].redacted is False

        consent_rows = {
            row.subject_principal_id: row
            for row in (await session.execute(select(HumanConsent))).scalars()
        }
        assert consent_rows[ids["old_guest_id"]].source_ip_hash is None
        assert consent_rows[ids["boundary_guest_id"]].source_ip_hash == "boundary-ip-hash"
        assert consent_rows[ids["fresh_guest_id"]].source_ip_hash == "fresh-ip-hash"

        old_guest = await session.get(Principal, ids["old_guest_id"])
        boundary_guest = await session.get(Principal, ids["boundary_guest_id"])
        fresh_guest = await session.get(Principal, ids["fresh_guest_id"])
        account = await session.get(Principal, ids["account_id"])
        assert old_guest is not None
        assert old_guest.deleted_at is not None
        assert _aware(old_guest.deleted_at) == _NOW
        assert old_guest.display_name is None
        assert boundary_guest is not None
        assert boundary_guest.deleted_at is None
        assert boundary_guest.display_name == "boundary guest"
        assert fresh_guest is not None
        assert fresh_guest.deleted_at is None
        assert account is not None
        assert account.deleted_at is None
        assert account.display_name == "old account"

        assert await _rating_snapshot(session) == before_rating
