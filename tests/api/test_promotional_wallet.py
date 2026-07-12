"""US-358: verified-account promotional grant and safe wallet API coverage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.core.ranked_ban import RankedBanKind
from padrino.db.models import (
    Game,
    OAuthIdentity,
    OperatorCompletionReserveLedgerEntry,
    Principal,
    PromotionalCreditLedgerEntry,
)
from padrino.db.repositories import (
    human_principals as principals_repo,
)
from padrino.db.repositories import (
    promotional_credit_ledger,
    ranked_bans,
)
from padrino.settings import Settings

_NOW = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)


def _settings(*, banned_principals: str = "", wallet_limit: int = 20) -> Settings:
    return Settings(
        _env_file=None,
        padrino_human_session_cookie_secure=False,
        padrino_rate_limit_human_per_minute=0,
        padrino_rate_limit_wallet_per_minute=wallet_limit,
        padrino_promotional_credit_abuse_banned_principal_ids=banned_principals,
    )


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = create_app(session_factory=session_factory, auth_required=False)
    app.state.auth_settings = _settings()
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _session_for(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str = "account",
    email_verified: bool = True,
    deleted: bool = False,
) -> tuple[str, Principal]:
    token = generate_session_token()
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(
            session, kind=kind, display_name="Wallet User"
        )
        if deleted:
            principal.deleted_at = _NOW
        if kind == "account":
            session.add(
                OAuthIdentity(
                    provider="wallet-test",
                    subject=f"wallet-subject-{principal.id}",
                    email="wallet@example.test",
                    email_verified=email_verified,
                    principal_id=principal.id,
                )
            )
        await principals_repo.create_session(
            session,
            principal_id=principal.id,
            raw_token=token,
            kind=kind,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
    return token, principal


def _set_session(client: AsyncClient, token: str) -> None:
    client.cookies.set(HUMAN_SESSION_COOKIE, token)


async def test_first_verified_wallet_grant_is_one_time_under_concurrent_requests(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal = await _session_for(session_factory)
    _set_session(client, token)

    first, second = await asyncio.gather(
        client.post("/human/wallet/grant"),
        client.post("/human/wallet/grant"),
    )

    assert {first.status_code, second.status_code} == {200, 201}
    assert {first.json()["grant"]["created"], second.json()["grant"]["created"]} == {False, True}
    assert first.json()["wallet"]["granted_micros"] == 5_000_000
    assert second.json()["wallet"]["available_micros"] == 5_000_000

    async with session_factory() as session:
        grants = await session.scalar(
            select(func.count())
            .select_from(PromotionalCreditLedgerEntry)
            .where(
                PromotionalCreditLedgerEntry.principal_id == principal.id,
                PromotionalCreditLedgerEntry.entry_type == "GRANT",
            )
        )
    assert grants == 1


async def test_wallet_projects_integer_totals_and_safe_recent_entries(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal = await _session_for(session_factory)
    _set_session(client, token)
    granted = await client.post("/human/wallet/grant")
    assert granted.status_code == 201

    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="wallet-projection-game",
            status="RUNNING",
        )
        session.add(game)
        await session.flush()
        reservation, _ = await promotional_credit_ledger.create_or_get_credit_reservation(
            session,
            principal_id=principal.id,
            lobby_id=None,
            game_id=game.id,
            estimate_hash="a" * 64,
            catalog_hash="b" * 64,
            rules_hash="c" * 64,
            reserved_micros=1_000_000,
            idempotency_key="wallet:reservation",
            now=_NOW,
        )
        for offset_seconds, (entry_type, amount, key, reason) in enumerate(
            (
                ("RESERVATION", -1_000_000, "wallet:hold", "game_admission"),
                ("CHARGE", -50_000, "wallet:charge", "openrouter_call"),
                ("RELEASE", 20_000, "wallet:release", "reservation_consumed"),
            ),
            start=1,
        ):
            await promotional_credit_ledger.append_credit_entry(
                session,
                principal_id=principal.id,
                reservation_id=reservation.id,
                game_id=game.id,
                entry_type=entry_type,
                amount_micros=amount,
                idempotency_key=key,
                reason=reason,
                now=_NOW + timedelta(seconds=offset_seconds),
            )
        reservation.consumed_micros = 50_000
        reservation.released_micros = 20_000
        await promotional_credit_ledger.append_completion_reserve_entry(
            session,
            game_id=game.id,
            entry_type="CHARGE",
            amount_micros=-7_000,
            estimate_miss_micros=7_000,
            model_slug="openai/example",
            idempotency_key="wallet:completion",
            reason="estimate_miss",
            now=_NOW,
        )

    response = await client.get("/human/wallet")

    assert response.status_code == 200
    wallet = response.json()
    assert wallet["granted_micros"] == 5_000_000
    assert wallet["available_micros"] == 3_970_000
    assert wallet["reserved_micros"] == 930_000
    assert wallet["spent_micros"] == 50_000
    assert wallet["completion_subsidy_micros"] == 7_000
    assert [entry["entry_type"] for entry in wallet["recent_entries"]][:3] == [
        "RELEASE",
        "CHARGE",
        "RESERVATION",
    ]
    assert all(
        "idempotency_key" not in entry and "principal_id" not in entry
        for entry in wallet["recent_entries"]
    )
    assert "openai/example" not in response.text
    assert "wallet:completion" not in response.text


@pytest.mark.parametrize(
    ("kind", "verified", "deleted", "expected"),
    [
        ("guest", False, False, "promotional_account_required"),
        ("account", False, False, "promotional_email_unverified"),
        ("account", True, True, "promotional_account_deleted"),
    ],
)
async def test_ineligible_principals_receive_a_stable_grant_refusal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    kind: str,
    verified: bool,
    deleted: bool,
    expected: str,
) -> None:
    token, principal = await _session_for(
        session_factory,
        kind=kind,
        email_verified=verified,
        deleted=deleted,
    )
    _set_session(client, token)

    response = await client.post("/human/wallet/grant")

    assert response.status_code == 403
    assert response.json()["detail"] == expected
    async with session_factory() as session:
        grants = await session.scalar(
            select(func.count())
            .select_from(PromotionalCreditLedgerEntry)
            .where(PromotionalCreditLedgerEntry.principal_id == principal.id)
        )
    assert grants == 0


async def test_credit_abuse_ban_is_a_distinct_server_owned_grant_fact(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal = await _session_for(session_factory)
    app.state.auth_settings = _settings(banned_principals=str(principal.id))
    _set_session(client, token)

    response = await client.post("/human/wallet/grant")

    assert response.status_code == 403
    assert response.json()["detail"] == "promotional_credit_abuse_banned"


async def test_ranked_ban_never_substitutes_for_the_credit_abuse_ban_fact(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal = await _session_for(session_factory)
    async with session_factory() as session, session.begin():
        await ranked_bans.issue_ban_for_principal(
            session,
            principal_id=principal.id,
            kind=RankedBanKind.BAN,
            reason="ranked-conduct-only",
            created_by="wallet-test",
            now=_NOW,
        )
    _set_session(client, token)

    response = await client.post("/human/wallet/grant")

    assert response.status_code == 201
    assert response.json()["grant"] == {
        "created": True,
        "reason": "promotional_grant_allowed",
    }


async def test_wallet_rate_limit_is_separate_and_grant_preserves_gdpr_tombstone_audit(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal = await _session_for(session_factory)
    app.state.auth_settings = _settings(wallet_limit=2)
    app.state.rate_limiter = RateLimiter(clock=lambda: 10.0)
    _set_session(client, token)

    assert (await client.post("/human/wallet/grant")).status_code == 201
    assert (await client.get("/human/wallet")).status_code == 200
    limited = await client.get("/human/wallet")
    assert limited.status_code == 429
    assert limited.json()["detail"] == "wallet_rate_limited"

    async with session_factory() as session, session.begin():
        tombstone = await principals_repo.anonymize_principal_for_delete(
            session,
            principal.id,
            now=_NOW,
        )
        assert tombstone is not None

    async with session_factory() as session:
        principal_row = await session.get(Principal, principal.id)
        ledger_rows = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry).where(
                        PromotionalCreditLedgerEntry.principal_id == principal.id
                    )
                )
            ).scalars()
        )
        subsidy_rows = list(
            (await session.execute(select(OperatorCompletionReserveLedgerEntry))).scalars()
        )
    assert principal_row is not None and principal_row.display_name is None
    assert principal_row.deleted_at is not None
    assert len(ledger_rows) == 1
    assert subsidy_rows == []
