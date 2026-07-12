"""US-363 retention coverage for OpenRouter flows and revoked envelopes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import OpenRouterPkceFlow, ProviderCredential
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.retention import (
    OpenRouterPkceFlowRetentionInfo,
    RevokedCredentialRetentionInfo,
    plan_openrouter_credential_retention,
)
from padrino.db.retention_executor import run_retention_executor
from padrino.settings import Settings

_NOW = datetime(2026, 7, 12, 23, 30, tzinfo=UTC)


def _settings(*, dry_run: bool) -> Settings:
    return Settings(
        _env_file=None,
        padrino_enable_retention=True,
        padrino_retention_dry_run=dry_run,
        padrino_openrouter_pkce_flow_retention_ttl_days=1,
        padrino_openrouter_revoked_credential_retention_ttl_days=7,
    )


def _flow(*, age: timedelta) -> OpenRouterPkceFlowRetentionInfo:
    return OpenRouterPkceFlowRetentionInfo(
        id=uuid.uuid4(),
        expires_at=_NOW - age,
    )


def _credential(
    *,
    age: timedelta,
    ciphertext_present: bool = True,
    nonce_present: bool = True,
) -> RevokedCredentialRetentionInfo:
    return RevokedCredentialRetentionInfo(
        id=uuid.uuid4(),
        revoked_at=_NOW - age,
        ciphertext_present=ciphertext_present,
        nonce_present=nonce_present,
    )


def test_openrouter_retention_plan_is_ttl_bound_and_idempotent() -> None:
    old_flow = _flow(age=timedelta(days=1, seconds=1))
    boundary_flow = _flow(age=timedelta(days=1))
    old_credential = _credential(age=timedelta(days=7, seconds=1))
    boundary_credential = _credential(age=timedelta(days=7))
    already_erased = _credential(
        age=timedelta(days=30),
        ciphertext_present=False,
        nonce_present=False,
    )

    plan = plan_openrouter_credential_retention(
        flows=[old_flow, boundary_flow],
        revoked_credentials=[old_credential, boundary_credential, already_erased],
        flow_retention_ttl_days=1,
        revoked_credential_retention_ttl_days=7,
        now=_NOW,
    )

    assert plan.pkce_flows_to_delete == [old_flow.id]
    assert plan.revoked_credentials_to_erase == [old_credential.id]


async def _seed_rows(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    principal = await principals_repo.create_principal(
        session,
        kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
    )
    old_flow = OpenRouterPkceFlow(
        principal_id=principal.id,
        state_hash="a" * 64,
        verifier_binding_hash="b" * 64,
        expires_at=_NOW - timedelta(days=1, seconds=1),
    )
    fresh_flow = OpenRouterPkceFlow(
        principal_id=principal.id,
        state_hash="c" * 64,
        verifier_binding_hash="d" * 64,
        expires_at=_NOW - timedelta(hours=12),
    )
    old_credential = ProviderCredential(
        principal_id=principal.id,
        provider="old-openrouter",
        ciphertext=b"legacy-revoked-ciphertext",
        nonce=b"legacy-revoked-nonce",
        key_version="v1",
        display_suffix="old1",
        label="Old credential",
        revoked_at=_NOW - timedelta(days=7, seconds=1),
    )
    fresh_credential = ProviderCredential(
        principal_id=principal.id,
        provider="fresh-openrouter",
        ciphertext=b"fresh-revoked-ciphertext",
        nonce=b"fresh-revoked-nonce",
        key_version="v1",
        display_suffix="new1",
        label="Fresh credential",
        revoked_at=_NOW - timedelta(days=6),
    )
    session.add_all([old_flow, fresh_flow, old_credential, fresh_credential])
    await session.flush()
    return old_flow.id, fresh_flow.id, old_credential.id, fresh_credential.id


@pytest.mark.asyncio
async def test_openrouter_retention_executor_dry_run_then_erases_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        old_flow_id, fresh_flow_id, old_credential_id, fresh_credential_id = await _seed_rows(
            session
        )

    dry_run = await run_retention_executor(
        session_factory,
        settings=_settings(dry_run=True),
        now=_NOW,
    )

    assert dry_run is not None
    assert dry_run.openrouter_pkce_flows_deleted == 0
    assert dry_run.revoked_credential_envelopes_erased == 0
    async with session_factory() as session:
        assert await session.get(OpenRouterPkceFlow, old_flow_id) is not None
        old_credential = await session.get(ProviderCredential, old_credential_id)
        assert old_credential is not None
        assert old_credential.ciphertext is not None
        assert old_credential.nonce is not None

    first = await run_retention_executor(
        session_factory,
        settings=_settings(dry_run=False),
        now=_NOW,
    )
    second = await run_retention_executor(
        session_factory,
        settings=_settings(dry_run=False),
        now=_NOW,
    )

    assert first is not None
    assert first.openrouter_pkce_flows_deleted == 1
    assert first.revoked_credential_envelopes_erased == 1
    assert second is not None
    assert second.openrouter_pkce_flows_deleted == 0
    assert second.revoked_credential_envelopes_erased == 0
    async with session_factory() as session:
        assert await session.get(OpenRouterPkceFlow, old_flow_id) is None
        assert await session.get(OpenRouterPkceFlow, fresh_flow_id) is not None
        old_credential = await session.get(ProviderCredential, old_credential_id)
        fresh_credential = await session.get(ProviderCredential, fresh_credential_id)
        assert old_credential is not None
        assert old_credential.ciphertext is None
        assert old_credential.nonce is None
        assert fresh_credential is not None
        assert fresh_credential.ciphertext == b"fresh-revoked-ciphertext"
        assert fresh_credential.nonce == b"fresh-revoked-nonce"
        remaining_flows = list((await session.execute(select(OpenRouterPkceFlow))).scalars())
        assert [flow.id for flow in remaining_flows] == [fresh_flow_id]
