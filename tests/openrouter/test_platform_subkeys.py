"""US-359 contracts for optional, account-scoped OpenRouter subkeys."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.db.models import OpenRouterPlatformSubkey, Principal
from padrino.db.repositories import human_principals as principals_repo
from padrino.openrouter.platform_subkeys import (
    MANAGEMENT_KEY_PURPOSE,
    OpenRouterManagementClient,
    OpenRouterPlatformSubkeyProvisioner,
    PlatformSubkeyManagementError,
    PlatformSubkeyRequest,
    platform_subkey_health,
    resolve_active_platform_subkey,
    resolve_platform_inference_key,
)
from padrino.settings import Settings

_NOW = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
_MANAGEMENT_KEY = "management-key-never-used-for-completions"
_INFERENCE_KEY = "inference-key-never-used-for-management"
_SUBKEY = "sk-or-v1-account-subkey-never-returned-1234"
_SECOND_SUBKEY = "sk-or-v1-rotated-subkey-never-returned-9876"
_FIRST_HASH = "a" * 64
_SECOND_HASH = "b" * 64


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _settings(*, management: bool = True, inference: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        padrino_openrouter_base_url="https://openrouter.example.test/api/v1",
        padrino_openrouter_credential_master_key_ring=SecretStr(f"v1:{_encoded_key(b'a')}"),
        padrino_openrouter_management_key_ref=(
            "env:PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY" if management else None
        ),
        padrino_openrouter_inference_key_ref=(
            "env:PADRINO_TEST_OPENROUTER_INFERENCE_KEY" if inference else None
        ),
    )


async def _principal(session_factory: async_sessionmaker[AsyncSession]) -> Principal:
    async with session_factory() as session, session.begin():
        return await principals_repo.create_principal(
            session,
            kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
            display_name="Promotional subkey tester",
        )


def _request(principal_id: uuid.UUID) -> PlatformSubkeyRequest:
    return PlatformSubkeyRequest(
        principal_id=principal_id,
        limit_micros=5_000_000,
        expires_at=_NOW + timedelta(days=30),
        allowed_models=("openai/gpt-5-mini", "qwen/qwen3-32b"),
    )


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_management_and_inference_credentials_are_purpose_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_INFERENCE_KEY", _INFERENCE_KEY)
    settings = _settings()

    assert resolve_platform_inference_key(settings) == _INFERENCE_KEY
    management = OpenRouterManagementClient.from_settings(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    assert management.purpose == MANAGEMENT_KEY_PURPOSE
    assert _MANAGEMENT_KEY not in repr(management)
    with pytest.raises(ValueError, match="only /keys"):
        await management._request("POST", "/chat/completions")
    await management.aclose()

    with pytest.raises(PlatformSubkeyManagementError, match="platform_inference_unavailable"):
        resolve_platform_inference_key(_settings(inference=False))
    with pytest.raises(PlatformSubkeyManagementError, match="subkey_management_unavailable"):
        OpenRouterManagementClient.from_settings(_settings(management=False))

    with pytest.raises(ValueError, match="must not share"):
        Settings(
            _env_file=None,
            padrino_openrouter_inference_key_ref="env:ONE_KEY",
            padrino_openrouter_management_key_ref="env:ONE_KEY",
        )


async def test_create_once_encrypts_the_one_time_subkey_and_keeps_safe_metadata(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_INFERENCE_KEY", _INFERENCE_KEY)
    principal = await _principal(session_factory)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/v1/keys"
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_MANAGEMENT_KEY}"
        body = json.loads(request.content)
        assert body["limit"] == 5.0
        assert body["expires_at"] == "2026-08-11T22:00:00Z"
        assert body["name"].startswith("padrino-promo-")
        return httpx.Response(
            200,
            json={
                "data": {
                    "key": _SUBKEY,
                    "hash": _FIRST_HASH,
                    "label": f"Padrino promotional account {_MANAGEMENT_KEY}",
                }
            },
        )

    client = OpenRouterManagementClient.from_settings(
        _settings(), client=_client(httpx.MockTransport(handler))
    )
    provisioner = OpenRouterPlatformSubkeyProvisioner(
        session_factory=session_factory,
        management_client=client,
        settings=_settings(),
        now=lambda: _NOW,
    )
    request = _request(principal.id)
    first = await provisioner.provision(request)
    second = await provisioner.provision(request)
    await client.aclose()

    assert first.id == second.id
    assert first.created is True
    assert second.created is False
    assert len(calls) == 1
    rendered = repr(first) + str(first)
    assert _SUBKEY not in rendered
    assert _MANAGEMENT_KEY not in rendered
    assert _INFERENCE_KEY not in rendered

    async with session_factory() as session:
        row = await session.scalar(
            select(OpenRouterPlatformSubkey).where(OpenRouterPlatformSubkey.id == first.id)
        )
        assert row is not None
        assert row.ciphertext is not None and row.ciphertext != _SUBKEY.encode()
        assert row.nonce is not None
        assert row.upstream_key_hash == _FIRST_HASH
        assert row.label is None
        assert row.allowed_models == ["openai/gpt-5-mini", "qwen/qwen3-32b"]
        assert _SUBKEY.encode() not in row.ciphertext
        assert _MANAGEMENT_KEY.encode() not in row.ciphertext
        assert (
            await resolve_active_platform_subkey(
                session,
                principal_id=principal.id,
                settings=_settings(),
                now=_NOW,
            )
            == _SUBKEY
        )


async def test_configured_upstream_allowlist_capability_is_sent_with_the_key_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    settings = _settings().model_copy(
        update={"padrino_openrouter_subkey_allowlist_field": "allowed_models"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["allowed_models"] == ["openai/gpt-5-mini", "qwen/qwen3-32b"]
        return httpx.Response(
            200,
            json={"data": {"key": _SUBKEY, "hash": _FIRST_HASH, "label": "safe"}},
        )

    client = OpenRouterManagementClient.from_settings(
        settings,
        client=_client(httpx.MockTransport(handler)),
    )
    created = await client.create_key(
        key_name="padrino-promo-allowlist",
        limit_micros=5_000_000,
        expires_at=_NOW + timedelta(days=30),
        allowed_models=("openai/gpt-5-mini", "qwen/qwen3-32b"),
    )
    await client.aclose()
    assert created.upstream_key_hash == _FIRST_HASH
    assert _SUBKEY not in repr(created)


async def test_timeout_after_create_reconciles_orphan_then_rotates_to_one_active_key(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    principal = await _principal(session_factory)
    post_attempts = 0
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        if request.method == "POST" and request.url.path == "/api/v1/keys":
            post_attempts += 1
            if post_attempts == 1:
                raise httpx.ReadTimeout("response lost after upstream create", request=request)
            return httpx.Response(
                200,
                json={"data": {"key": _SECOND_SUBKEY, "hash": _SECOND_HASH, "label": "safe"}},
            )
        if request.method == "GET" and request.url.path == "/api/v1/keys":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "hash": _FIRST_HASH,
                            "name": "padrino-promo-timeout-reconcile",
                            "label": "safe",
                            "disabled": False,
                        }
                    ]
                },
            )
        if request.method == "DELETE" and request.url.path == f"/api/v1/keys/{_FIRST_HASH}":
            deleted.append(_FIRST_HASH)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = OpenRouterManagementClient.from_settings(
        _settings(), client=_client(httpx.MockTransport(handler))
    )
    provisioner = OpenRouterPlatformSubkeyProvisioner(
        session_factory=session_factory,
        management_client=client,
        settings=_settings(),
        now=lambda: _NOW,
        key_name=lambda _: "padrino-promo-timeout-reconcile",
    )
    result = await provisioner.provision(_request(principal.id))
    await client.aclose()

    assert result.reconciled_orphan is True
    assert post_attempts == 2
    assert deleted == [_FIRST_HASH]
    async with session_factory() as session:
        active = list(
            (
                await session.execute(
                    select(OpenRouterPlatformSubkey).where(
                        OpenRouterPlatformSubkey.principal_id == principal.id,
                        OpenRouterPlatformSubkey.status == "ACTIVE",
                    )
                )
            ).scalars()
        )
    assert [row.upstream_key_hash for row in active] == [_SECOND_HASH]


async def test_rotation_and_upstream_denials_do_not_expose_or_reenable_a_subkey(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    principal = await _principal(session_factory)
    created = 0
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if request.method == "POST":
            created += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "key": _SUBKEY if created == 1 else _SECOND_SUBKEY,
                        "hash": _FIRST_HASH if created == 1 else _SECOND_HASH,
                        "label": "safe",
                    }
                },
            )
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if request.method == "PATCH":
            return httpx.Response(429, json={"error": "rate limited"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = OpenRouterManagementClient.from_settings(
        _settings(), client=_client(httpx.MockTransport(handler))
    )
    provisioner = OpenRouterPlatformSubkeyProvisioner(
        session_factory=session_factory,
        management_client=client,
        settings=_settings(),
        now=lambda: _NOW,
    )
    request = _request(principal.id)
    first = await provisioner.provision(request)
    second = await provisioner.provision(request, rotate=True)
    revoked_upstream = await provisioner.disable_for_principal(principal.id)
    await client.aclose()

    assert first.id != second.id
    assert deleted == [_FIRST_HASH]
    assert revoked_upstream is False
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(OpenRouterPlatformSubkey)
                    .where(OpenRouterPlatformSubkey.principal_id == principal.id)
                    .order_by(OpenRouterPlatformSubkey.provisioned_at)
                )
            ).scalars()
        )
    statuses = {row.id: row.status for row in rows}
    assert statuses == {first.id: "REVOKED", second.id: "DISABLED"}
    disabled = next(row for row in rows if row.id == second.id)
    assert disabled.ciphertext is None
    assert disabled.nonce is None


async def test_management_403_429_and_absence_fall_back_without_secret_diagnostics(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_INFERENCE_KEY", _INFERENCE_KEY)
    principal = await _principal(session_factory)

    for status_code, reason in (
        (403, "subkey_management_forbidden"),
        (429, "subkey_management_throttled"),
    ):
        client = OpenRouterManagementClient.from_settings(
            _settings(),
            client=_client(httpx.MockTransport(lambda _, code=status_code: httpx.Response(code))),
        )
        provisioner = OpenRouterPlatformSubkeyProvisioner(
            session_factory=session_factory,
            management_client=client,
            settings=_settings(),
            now=lambda: _NOW,
        )
        with pytest.raises(PlatformSubkeyManagementError, match=reason) as exc_info:
            await provisioner.provision(_request(principal.id))
        assert _MANAGEMENT_KEY not in str(exc_info.value)
        assert _INFERENCE_KEY not in str(exc_info.value)
        await client.aclose()

    no_management = platform_subkey_health(_settings(management=False))
    assert no_management.mode == "server_inference_fallback"
    assert no_management.management_configured is False
    assert no_management.inference_configured is True

    monkeypatch.delenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY")
    unavailable_management = platform_subkey_health(_settings())
    assert unavailable_management.mode == "server_inference_fallback"
    assert unavailable_management.management_configured is True
    assert unavailable_management.management_available is False


async def test_health_projects_subkey_and_server_inference_fallback_without_references(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_INFERENCE_KEY", _INFERENCE_KEY)
    app = create_app(session_factory=session_factory, auth_required=False)
    app.state.auth_settings = _settings(management=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/healthz/openrouter-funding")
    assert response.status_code == 200
    assert response.json() == {
        "mode": "server_inference_fallback",
        "management_configured": False,
        "management_available": False,
        "inference_configured": True,
        "inference_available": True,
        "encryption_available": True,
        "upstream_allowlist_enabled": False,
    }
    assert _INFERENCE_KEY not in response.text
    assert "PADRINO_TEST_OPENROUTER_INFERENCE_KEY" not in response.text
