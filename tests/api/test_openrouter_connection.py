"""US-355: private OpenRouter PKCE connection API contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.api.human_consent import record_consent
from padrino.db.models import OpenRouterPkceFlow, ProviderCredential
from padrino.db.repositories import human_principals as principals_repo
from padrino.openrouter.connection import OPENROUTER_PKCE_VERIFIER_COOKIE
from padrino.settings import Settings

_NOW = datetime.now(UTC)  # real clock: API checks sessions/staleness against it
_OPENROUTER_ORIGIN = "https://openrouter.example.test"
_OPENROUTER_BASE_URL = f"{_OPENROUTER_ORIGIN}/api/v1"
_CALLBACK_URL = "https://padrino.example.test/human/openrouter/connect/callback"
_USER_KEY = "sk-or-v1-connection-secret-never-returned-9876"
_SECOND_USER_KEY = "sk-or-v1-second-secret-never-returned-4321"
_MANAGEMENT_KEY = "management-key-never-returned"
_UPSTREAM_KEY_HASH = "a" * 64


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _settings(*, management_key: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        padrino_human_session_cookie_secure=False,
        padrino_openrouter_base_url=_OPENROUTER_BASE_URL,
        padrino_openrouter_credential_master_key_ring=SecretStr(f"v1:{_encoded_key(b'a')}"),
        padrino_openrouter_connect_callback_url=_CALLBACK_URL,
        padrino_openrouter_connect_state_signing_key=SecretStr(
            "openrouter-connect-state-signing-key-for-tests"
        ),
        padrino_openrouter_management_key_ref=(
            "env:PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY" if management_key else None
        ),
    )


@pytest.fixture
def app(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PADRINO_TEST_OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT_KEY)
    app = create_app(session_factory=session_factory, auth_required=False)
    app.state.auth_settings = _settings()
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _account_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, object]:
    now = _NOW
    raw_token = generate_session_token()
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(
            session,
            kind=principals_repo.PRINCIPAL_KIND_ACCOUNT,
            display_name="Connection Tester",
        )
        await principals_repo.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw_token,
            kind=principals_repo.SESSION_KIND_ACCOUNT,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await record_consent(
            session,
            subject_principal_id=principal.id,
            settings=_settings(),
            accepted_at=now,
        )
    return raw_token, principal.id


def _http_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    return lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _set_human_session(client: AsyncClient, token: str) -> None:
    """Set the browser session cookie without deprecated per-request cookies."""
    client.cookies.set(HUMAN_SESSION_COOKIE, token)


async def _start_connection(client: AsyncClient, token: str) -> str:
    _set_human_session(client, token)
    response = await client.get(
        "/human/openrouter/connect/start",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith(f"{_OPENROUTER_ORIGIN}/auth?")
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["callback_url"] == [_CALLBACK_URL]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["state"]) == 1
    verifier = client.cookies.get(OPENROUTER_PKCE_VERIFIER_COOKIE)
    assert verifier is not None
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert params["code_challenge"] == [expected_challenge]
    return params["state"][0]


async def test_connect_callback_uses_s256_exchanges_validates_encrypts_and_disconnects(
    client: AsyncClient,
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _account_session(session_factory)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/auth/keys":
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["code"] == "issued-code"
            assert body["code_challenge_method"] == "S256"
            assert isinstance(body["code_verifier"], str)
            return httpx.Response(200, json={"key": _USER_KEY})
        if request.url.path == "/api/v1/key":
            assert request.headers["authorization"] == f"Bearer {_USER_KEY}"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "hash": _UPSTREAM_KEY_HASH,
                        "label": "Padrino personal key ...9876",
                        "limit_remaining": 12.5,
                    }
                },
            )
        if request.url.path == f"/api/v1/keys/{_UPSTREAM_KEY_HASH}":
            assert request.method == "DELETE"
            assert request.headers["authorization"] == f"Bearer {_MANAGEMENT_KEY}"
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(f"unexpected OpenRouter request: {request.method} {request.url}")

    app.state.openrouter_connection_http_client_factory = _http_client_factory(handler)

    _set_human_session(client, token)
    before = await client.get("/human/openrouter/connect")
    assert before.status_code == 200
    assert before.json() == {
        "connected": False,
        "label": None,
        "display_suffix": None,
        "validated_at": None,
        "credit_capability": None,
    }

    state = await _start_connection(client, token)
    callback = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": state},
    )
    assert callback.status_code == 200
    assert callback.json() == {
        "connected": True,
        "label": "Padrino personal key ...9876",
        "display_suffix": "9876",
        "validated_at": callback.json()["validated_at"],
        "credit_capability": "openrouter_user_key",
    }
    assert _USER_KEY not in callback.text

    async with session_factory() as session:
        credential = (
            await session.execute(
                select(ProviderCredential).where(ProviderCredential.principal_id == principal_id)
            )
        ).scalar_one()
        assert credential.ciphertext is not None
        assert credential.ciphertext != _USER_KEY.encode()
        assert credential.nonce is not None
        assert credential.last_validated_at is not None
        assert credential.display_suffix == "9876"

    status = await client.get("/human/openrouter/connect")
    assert status.status_code == 200
    assert status.json() == callback.json()

    disconnected = await client.post("/human/openrouter/connect/disconnect")
    assert disconnected.status_code == 204
    assert any(request.method == "DELETE" for request in calls)

    async with session_factory() as session:
        erased = (
            await session.execute(
                select(ProviderCredential).where(ProviderCredential.principal_id == principal_id)
            )
        ).scalar_one()
        assert erased.revoked_at is not None
        assert erased.ciphertext is None
        assert erased.nonce is None


async def test_connection_callback_failures_are_bound_single_use_and_secret_safe(
    client: AsyncClient,
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_token, first_principal_id = await _account_session(session_factory)
    second_token, _ = await _account_session(session_factory)
    exchanges = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        if request.url.path == "/api/v1/auth/keys":
            exchanges += 1
            return httpx.Response(200, json={"key": _USER_KEY})
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"label": "Safe ...9876"}})
        raise AssertionError(f"unexpected OpenRouter request: {request.method} {request.url}")

    app.state.openrouter_connection_http_client_factory = _http_client_factory(handler)

    state = await _start_connection(client, first_token)
    csrf = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": f"{state}x"},
    )
    assert csrf.status_code == 400
    assert csrf.json()["detail"] == "openrouter_connection_state_invalid"
    assert exchanges == 0

    _set_human_session(client, second_token)
    wrong_session = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": state},
    )
    assert wrong_session.status_code == 400
    assert wrong_session.json()["detail"] == "openrouter_connection_state_invalid"
    assert exchanges == 0

    async with session_factory() as session, session.begin():
        flow = (
            await session.execute(
                select(OpenRouterPkceFlow).where(
                    OpenRouterPkceFlow.principal_id == first_principal_id
                )
            )
        ).scalar_one()
        flow.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    _set_human_session(client, first_token)
    expired = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": state},
    )
    assert expired.status_code == 400
    assert expired.json()["detail"] == "openrouter_connection_replayed"
    assert exchanges == 0

    fresh_state = await _start_connection(client, first_token)
    fresh_verifier = client.cookies.get(OPENROUTER_PKCE_VERIFIER_COOKIE)
    assert fresh_verifier is not None
    connected = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": fresh_state},
    )
    assert connected.status_code == 200
    assert exchanges == 1

    client.cookies.set(
        OPENROUTER_PKCE_VERIFIER_COOKIE,
        fresh_verifier,
        domain="testserver.local",
        path="/",
    )
    replay = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "issued-code", "state": fresh_state},
    )
    assert replay.status_code == 400
    assert replay.json()["detail"] == "openrouter_connection_replayed"
    assert exchanges == 1

    second_state = await _start_connection(client, first_token)
    duplicate = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "second-code", "state": second_state},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "openrouter_connection_store_failed"
    assert _USER_KEY not in duplicate.text

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ProviderCredential).where(
                        ProviderCredential.principal_id == first_principal_id,
                        ProviderCredential.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1


async def test_connection_provider_failure_and_callback_allowlist_fail_closed(
    client: AsyncClient,
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, principal_id = await _account_session(session_factory)

    def rejected_exchange(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/keys"
        return httpx.Response(403, json={"error": "invalid code"})

    app.state.openrouter_connection_http_client_factory = _http_client_factory(rejected_exchange)
    state = await _start_connection(client, token)
    failed = await client.get(
        "/human/openrouter/connect/callback",
        params={"code": "rejected-code", "state": state},
    )
    assert failed.status_code == 400
    assert failed.json()["detail"] == "openrouter_connection_exchange_failed"
    assert _USER_KEY not in failed.text

    async with session_factory() as session:
        assert (
            await session.execute(
                select(ProviderCredential).where(ProviderCredential.principal_id == principal_id)
            )
        ).scalars().all() == []

    app.state.auth_settings = _settings().model_copy(
        update={
            "padrino_openrouter_connect_callback_url": "https://padrino.example.test/not-a-callback"
        }
    )
    refused = await client.get("/human/openrouter/connect/start", follow_redirects=False)
    assert refused.status_code == 503
    assert refused.json()["detail"] == "openrouter_connection_unavailable"
