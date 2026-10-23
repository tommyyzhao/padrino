"""US-363 contracts for secret-safe OpenRouter GDPR and operations surfaces."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.db.models import (
    CatalogSnapshot,
    Game,
    OpenRouterPkceFlow,
    OpenRouterPlatformSubkey,
    OperatorCompletionReserveLedgerEntry,
    Principal,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
    ProviderCredential,
)
from padrino.db.repositories import human_principals as principals_repo
from padrino.settings import Settings

_NOW = datetime.now(UTC)  # real clock: API checks sessions/staleness against it
_SENTINEL = "sk-or-us363-sentinel-must-never-leave-private-storage"
_UPSTREAM_HASH = "a" * 64
_REQUEST_HASH = "b" * 64
_ESTIMATE_HASH = "c" * 64
_CATALOG_HASH = "d" * 64
_RULES_HASH = "e" * 64
_FRONTEND_TEXT_FIXTURE_SUFFIXES = frozenset(
    {".css", ".html", ".js", ".json", ".md", ".svelte", ".ts"}
)


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        padrino_human_session_cookie_secure=False,
        padrino_openrouter_catalog_sync_interval_seconds=3600,
        padrino_openrouter_completion_reserve_global_micros=100,
        padrino_openrouter_credential_master_key_ring=SecretStr(f"v1:{_encoded_key(b'a')}"),
        padrino_openrouter_management_key_ref="env:US363_MANAGEMENT_REFERENCE",
    )


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = create_app(session_factory=session_factory, auth_required=False)
    app.state.auth_settings = _settings()
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


async def _account(session: AsyncSession) -> tuple[str, Principal]:
    token = generate_session_token()
    principal = await principals_repo.create_principal(
        session,
        kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
        display_name="GDPR OpenRouter Tester",
    )
    await principals_repo.create_session(
        session,
        principal_id=principal.id,
        raw_token=token,
        kind=principals_repo.SESSION_KIND_ACCOUNT,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    return token, principal


async def _seed_private_openrouter_facts(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    game = Game(
        ruleset_id="mini7_v1",
        game_seed="us363-required-charge-audit",
        status="COMPLETED",
    )
    session.add(game)
    session.add(
        CatalogSnapshot(
            content_hash=_CATALOG_HASH,
            fetched_at=_NOW,
            source_etag=None,
            source_version=None,
            status="COMPLETE",
            completed_at=_NOW,
        )
    )
    await session.flush()

    cancellable = PromotionalCreditReservation(
        principal_id=principal_id,
        lobby_id=None,
        game_id=None,
        estimate_hash=_ESTIMATE_HASH,
        catalog_hash=_CATALOG_HASH,
        rules_hash=_RULES_HASH,
        reserved_micros=120,
        consumed_micros=0,
        released_micros=0,
        status="ACTIVE",
        idempotency_key="us363-cancellable-reservation",
        reserved_at=_NOW,
        updated_at=_NOW,
        terminal_at=None,
    )
    settled = PromotionalCreditReservation(
        principal_id=principal_id,
        lobby_id=None,
        game_id=game.id,
        estimate_hash=_ESTIMATE_HASH,
        catalog_hash=_CATALOG_HASH,
        rules_hash=_RULES_HASH,
        reserved_micros=50,
        consumed_micros=50,
        released_micros=0,
        status="SETTLED",
        idempotency_key="us363-required-charge-reservation",
        reserved_at=_NOW,
        updated_at=_NOW,
        terminal_at=_NOW,
    )
    session.add_all([cancellable, settled])
    await session.flush()
    session.add_all(
        [
            ProviderCredential(
                principal_id=principal_id,
                provider="openrouter",
                ciphertext=_SENTINEL.encode("utf-8"),
                nonce=b"private-nonce",
                key_version="v1",
                display_suffix="tinel",
                label="Personal OpenRouter connection",
                last_validated_at=_NOW,
            ),
            OpenRouterPlatformSubkey(
                principal_id=principal_id,
                ciphertext=_SENTINEL.encode("utf-8"),
                nonce=b"private-subkey-nonce",
                key_version="v1",
                upstream_key_hash=_UPSTREAM_HASH,
                key_name="padrino-promo-us363",
                display_suffix="tinel",
                label="Promotional OpenRouter key",
                limit_micros=500,
                expires_at=_NOW + timedelta(days=1),
                allowed_models=["openai/gpt-5-mini"],
                allowlist_enforced_upstream=False,
                request_hash=_REQUEST_HASH,
                status="ACTIVE",
                provisioned_at=_NOW,
            ),
            OpenRouterPkceFlow(
                principal_id=principal_id,
                state_hash="f" * 64,
                verifier_binding_hash="0" * 64,
                expires_at=_NOW + timedelta(minutes=10),
            ),
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=None,
                game_id=None,
                entry_type="GRANT",
                amount_micros=5_000_000,
                idempotency_key="us363-grant",
                reason="verified_account_promotion",
                created_at=_NOW,
            ),
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=cancellable.id,
                game_id=None,
                entry_type="RESERVATION",
                amount_micros=-120,
                idempotency_key="us363-cancellable-hold",
                reason="game_admission",
                created_at=_NOW,
            ),
            PromotionalCreditLedgerEntry(
                principal_id=principal_id,
                reservation_id=settled.id,
                game_id=game.id,
                entry_type="CHARGE",
                amount_micros=-50,
                idempotency_key="us363-required-charge",
                reason="openrouter_call",
                created_at=_NOW,
            ),
            OperatorCompletionReserveLedgerEntry(
                game_id=game.id,
                entry_type="CHARGE",
                amount_micros=-40,
                estimate_miss_micros=40,
                model_slug="openai/gpt-5-mini",
                idempotency_key="us363-completion-charge",
                reason="custom_game_estimate_miss",
                created_at=_NOW,
            ),
        ]
    )
    await session.flush()
    return cancellable.id, settled.id


@pytest.mark.asyncio
async def test_export_and_delete_are_safe_for_connections_credit_and_required_audit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        token, principal = await _account(session)
        cancellable_id, settled_id = await _seed_private_openrouter_facts(
            session,
            principal_id=principal.id,
        )

    exported = await client.get(
        "/human/me/export",
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["openrouter_connections"] == [
        {
            "provider": "openrouter",
            "label": "Personal OpenRouter connection",
            "display_suffix": "tinel",
            "created_at": body["openrouter_connections"][0]["created_at"],
            "last_validated_at": body["openrouter_connections"][0]["last_validated_at"],
            "revoked_at": None,
        }
    ]
    assert {
        (entry["entry_type"], entry["amount_micros"]) for entry in body["wallet_ledger_entries"]
    } == {
        ("GRANT", 5_000_000),
        ("RESERVATION", -120),
        ("CHARGE", -50),
    }
    rendered_export = json.dumps(body, sort_keys=True)
    for forbidden in (
        _SENTINEL,
        _UPSTREAM_HASH,
        "ciphertext",
        "nonce",
        "key_version",
        "idempotency_key",
        "state_hash",
        "verifier_binding_hash",
    ):
        assert forbidden not in rendered_export

    deleted = await client.post(
        "/human/me/delete",
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert deleted.status_code == 204, deleted.text
    async with session_factory() as session:
        credential = (
            await session.execute(
                select(ProviderCredential).where(ProviderCredential.principal_id == principal.id)
            )
        ).scalar_one()
        subkey = (
            await session.execute(
                select(OpenRouterPlatformSubkey).where(
                    OpenRouterPlatformSubkey.principal_id == principal.id
                )
            )
        ).scalar_one()
        assert credential.revoked_at is not None
        assert credential.ciphertext is None
        assert credential.nonce is None
        assert subkey.status == "REVOKED"
        assert subkey.ciphertext is None
        assert subkey.nonce is None
        assert (
            await session.execute(
                select(OpenRouterPkceFlow).where(OpenRouterPkceFlow.principal_id == principal.id)
            )
        ).scalars().all() == []

        cancelled = await session.get(PromotionalCreditReservation, cancellable_id)
        required_charge = await session.get(PromotionalCreditReservation, settled_id)
        assert cancelled is not None
        assert cancelled.status == "CANCELLED"
        assert cancelled.released_micros == 120
        assert required_charge is not None
        assert required_charge.status == "SETTLED"
        charge_rows = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry)
                    .where(PromotionalCreditLedgerEntry.principal_id == principal.id)
                    .order_by(
                        PromotionalCreditLedgerEntry.created_at, PromotionalCreditLedgerEntry.id
                    )
                )
            ).scalars()
        )
        assert {(row.entry_type, row.amount_micros) for row in charge_rows} == {
            ("GRANT", 5_000_000),
            ("RESERVATION", -120),
            ("CHARGE", -50),
            ("RELEASE", 120),
        }


@pytest.mark.asyncio
async def test_openrouter_operations_health_is_aggregate_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        _token, principal = await _account(session)
        await _seed_private_openrouter_facts(session, principal_id=principal.id)

    response = await client.get("/healthz/openrouter-operations")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["catalog"] == {
        "content_hash": _CATALOG_HASH,
        "fetched_at": body["catalog"]["fetched_at"],
        "state": "current",
        "is_stale": False,
    }
    assert body["encryption_ready"] is True
    assert body["management"] == {
        "configured": True,
        "available": False,
        "mode": "platform_inference_unavailable",
    }
    assert body["promotional_credit"] == {
        "liability_micros": 4_999_830,
        "active_reservation_micros": 120,
        "active_reservation_count": 1,
    }
    assert body["completion_reserve"] == {
        "global_cap_micros": 100,
        "charged_micros": 40,
        "remaining_micros": 60,
    }
    rendered = json.dumps(body, sort_keys=True)
    for forbidden in (_SENTINEL, _UPSTREAM_HASH, str(principal.id), "management_key"):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_sentinel_secret_scanner_covers_player_archive_and_fixture_surfaces(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with session_factory() as session, session.begin():
        token, principal = await _account(session)
        await _seed_private_openrouter_facts(session, principal_id=principal.id)

    with caplog.at_level(logging.INFO):
        logging.getLogger("padrino.us363").info("OpenRouter connection rejected safely")
        export_response = await client.get(
            "/human/me/export",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        status_response = await client.get(
            "/human/openrouter/connect",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        operations_response = await client.get("/healthz/openrouter-operations")

    assert export_response.status_code == 200
    assert status_response.status_code == 200
    assert operations_response.status_code == 200
    replay_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "PublicMessageSubmitted",
                    "payload": {"content_ref": "sha256:sidecar-only"},
                }
            ]
        },
        sort_keys=True,
    )
    analytics_json = json.dumps(
        {"analytics": {"event_count": 1, "public_messages": 1}}, sort_keys=True
    )
    frontend_fixtures = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("web/dashboard/tests").rglob("*")
        if path.is_file() and path.suffix in _FRONTEND_TEXT_FIXTURE_SUFFIXES
    )
    surfaces = {
        "logs": caplog.text,
        "events": replay_json,
        "replay": replay_json,
        "export": export_response.text,
        "api_reads": status_response.text + operations_response.text,
        "analytics": analytics_json,
        "frontend_fixtures": frontend_fixtures,
    }
    leaked = [name for name, rendered in surfaces.items() if _SENTINEL in rendered]
    assert leaked == []
