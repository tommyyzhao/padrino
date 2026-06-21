"""Tests for optional OAuth sign-in, one provider (US-129).

The provider network round-trip is stubbed end-to-end (``set_resolve_user_info``)
so no live provider is contacted. Covers: the start redirect carries CSRF state +
PKCE, the callback validates state and find-or-creates an account principal keyed
by (provider, subject), a repeat sign-in resolves the SAME account, a signed-in
guest is upgraded in place (its sessions re-point to the account), the routes 503
when no provider is configured, and provider secrets/tokens are never persisted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from http.cookies import SimpleCookie
from time import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from authlib.jose import JsonWebKey, jwt
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.api.oauth import (
    OAuthUserInfo,
    reset_clock,
    reset_oauth_io,
    reset_resolve_user_info,
    set_clock,
    set_exchange_token,
    set_fetch_jwks,
    set_resolve_user_info,
)
from padrino.api.routes.human import OAUTH_STATE_COOKIE, OAUTH_VERIFIER_COOKIE
from padrino.db.models import OAuthConsumedFlow, OAuthIdentity, Principal
from padrino.db.repositories import human_principals as principals_repo
from padrino.settings import Settings, get_settings

_OAUTH_ENV = {
    "PADRINO_OAUTH_PROVIDER": "google",
    "PADRINO_OAUTH_CLIENT_ID": "client-id-123",
    "PADRINO_OAUTH_CLIENT_SECRET": "super-secret",
    "PADRINO_OAUTH_AUTHORIZE_URL": "https://provider.example/authorize",
    "PADRINO_OAUTH_TOKEN_URL": "https://provider.example/token",
    "PADRINO_OAUTH_USERINFO_URL": "https://provider.example/userinfo",
    "PADRINO_OAUTH_REDIRECT_URL": "https://app.example/human/oauth/google/callback",
    "PADRINO_OAUTH_ISSUER": "https://provider.example",
    "PADRINO_OAUTH_JWKS_URL": "https://provider.example/jwks.json",
    "PADRINO_OAUTH_STATE_SIGNING_KEY": "dedicated-server-state-key",
    "PADRINO_HUMAN_SESSION_COOKIE_SECURE": "false",
    "CEREBRAS_API_KEY": "test-cerebras-key",
}

_OIDC_KEY = JsonWebKey.generate_key("RSA", 2048, {"kid": "test-key"}, is_private=True)


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in _OAUTH_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_resolver() -> Iterator[None]:
    yield
    reset_resolve_user_info()
    reset_oauth_io()
    reset_clock()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _cookie(resp_headers: object, name: str) -> str | None:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{name}="):
            jar.load(raw)
    if name not in jar:
        return None
    return jar[name].value


def _stub_subject(subject: str, *, name: str | None = "Alice") -> None:
    async def _resolve(
        config: object, *, code: str, code_verifier: str, nonce: str
    ) -> OAuthUserInfo:
        return OAuthUserInfo(subject=subject, display_name=name)

    set_resolve_user_info(_resolve)


def _query_param(location: str, name: str) -> str:
    values = parse_qs(urlsplit(location).query)[name]
    assert len(values) == 1
    return values[0]


def _jwks() -> dict[str, object]:
    return {"keys": [_OIDC_KEY.as_dict(is_private=False)]}


def _signed_id_token(
    *,
    subject: str = "subject-token",
    nonce: str,
    aud: str | list[str] = "client-id-123",
    iss: str = "https://provider.example",
    exp: int | None = None,
    include_exp: bool = True,
    azp: str | None = None,
    iat: int | None = None,
) -> str:
    claims: dict[str, Any] = {
        "iss": iss,
        "sub": subject,
        "aud": aud,
        "nonce": nonce,
        "name": "Token Alice",
        "iat": iat if iat is not None else int(time()),
    }
    if azp is not None:
        claims["azp"] = azp
    if include_exp:
        claims["exp"] = exp if exp is not None else int(time()) + 3600
    token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, _OIDC_KEY)
    assert isinstance(token, bytes)
    return token.decode("ascii")


async def _callback_cookies(
    client: AsyncClient, *, human_session: str | None = None
) -> tuple[str, str, str]:
    cookies = {HUMAN_SESSION_COOKIE: human_session} if human_session is not None else None
    start = await client.get("/human/oauth/google/start", cookies=cookies)
    state = _cookie(start.headers, OAUTH_STATE_COOKIE)
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert state is not None and verifier is not None
    nonce = _query_param(start.headers["location"], "nonce")
    return state, verifier, nonce


def _stub_valid_token(nonce: str, *, subject: str = "subject-token") -> None:
    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": _signed_id_token(subject=subject, nonce=nonce)}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)


def _oauth_settings_with_max_token_age(max_token_age_seconds: int | None) -> Settings:
    return Settings(
        _env_file=None,
        padrino_oauth_provider="google",
        padrino_oauth_client_id="client-id-123",
        padrino_oauth_client_secret="super-secret",
        padrino_oauth_authorize_url="https://provider.example/authorize",
        padrino_oauth_token_url="https://provider.example/token",
        padrino_oauth_userinfo_url="https://provider.example/userinfo",
        padrino_oauth_redirect_url="https://app.example/human/oauth/google/callback",
        padrino_oauth_issuer="https://provider.example",
        padrino_oauth_jwks_url="https://provider.example/jwks.json",
        padrino_oauth_state_signing_key="dedicated-server-state-key",
        padrino_oauth_max_token_age_seconds=max_token_age_seconds,
        padrino_human_session_cookie_secure=False,
        cerebras_api_key="test-cerebras-key",
    )


@pytest.mark.asyncio
async def test_start_redirects_with_state_and_pkce(client: AsyncClient) -> None:
    resp = await client.get("/human/oauth/google/start")
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://provider.example/authorize")
    assert "state=" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert _cookie(resp.headers, OAUTH_STATE_COOKIE) is not None
    assert _cookie(resp.headers, OAUTH_VERIFIER_COOKIE) is not None


@pytest.mark.asyncio
async def test_callback_creates_account_keyed_by_subject(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    start = await client.get("/human/oauth/google/start")
    state = _cookie(start.headers, OAUTH_STATE_COOKIE)
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert state is not None and verifier is not None

    _stub_subject("subject-abc")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "account"
    assert _cookie(resp.headers, HUMAN_SESSION_COOKIE) is not None

    async with session_factory() as session:
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
        principals = (await session.execute(select(Principal))).scalars().all()
    assert len(identities) == 1
    assert identities[0].provider == "google"
    assert identities[0].subject == "subject-abc"
    # The provider secret/token is never stored anywhere on the identity row.
    assert "super-secret" not in repr(identities[0].__dict__)
    assert len(principals) == 1
    assert principals[0].kind == "account"


@pytest.mark.asyncio
async def test_repeat_signin_resolves_same_account(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def _login() -> str:
        start = await client.get("/human/oauth/google/start")
        state = _cookie(start.headers, OAUTH_STATE_COOKIE)
        verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
        assert state is not None and verifier is not None
        _stub_subject("subject-repeat")
        resp = await client.get(
            "/human/oauth/google/callback",
            params={"code": "auth-code", "state": state},
            cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
        )
        assert resp.status_code == 200
        return str(resp.json()["principal_id"])

    first = await _login()
    second = await _login()
    assert first == second

    async with session_factory() as session:
        principals = (await session.execute(select(Principal))).scalars().all()
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
    assert len(principals) == 1
    assert len(identities) == 1


@pytest.mark.asyncio
async def test_signed_in_guest_upgraded_in_place(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    guest = await client.post("/human/guest")
    guest_token = _cookie(guest.headers, HUMAN_SESSION_COOKIE)
    guest_id = guest.json()["principal_id"]
    assert guest_token is not None

    start = await client.get("/human/oauth/google/start")
    state = _cookie(start.headers, OAUTH_STATE_COOKIE)
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert state is not None and verifier is not None

    _stub_subject("subject-upgrade")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={
            OAUTH_STATE_COOKIE: state,
            OAUTH_VERIFIER_COOKIE: verifier,
            HUMAN_SESSION_COOKIE: guest_token,
        },
    )
    assert resp.status_code == 200
    # The SAME principal is upgraded in place (no new principal created).
    assert resp.json()["principal_id"] == guest_id
    assert resp.json()["kind"] == "account"

    async with session_factory() as session:
        principals = (await session.execute(select(Principal))).scalars().all()
    assert len(principals) == 1
    assert principals[0].kind == "account"

    # The original guest cookie still resolves (its session re-points in place).
    me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert me.status_code == 200
    assert me.json()["kind"] == "account"


@pytest.mark.asyncio
async def test_callback_rejects_state_mismatch(client: AsyncClient) -> None:
    start = await client.get("/human/oauth/google/start")
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert verifier is not None
    _stub_subject("subject-x")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "c", "state": "attacker-state"},
        cookies={OAUTH_STATE_COOKIE: "real-state", OAUTH_VERIFIER_COOKIE: verifier},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_state_replayed_from_another_guest_session(
    client: AsyncClient,
) -> None:
    guest_a = await client.post("/human/guest")
    guest_a_token = _cookie(guest_a.headers, HUMAN_SESSION_COOKIE)
    assert guest_a_token is not None
    state, verifier, _ = await _callback_cookies(client, human_session=guest_a_token)

    guest_b = await client.post("/human/guest")
    guest_b_token = _cookie(guest_b.headers, HUMAN_SESSION_COOKIE)
    assert guest_b_token is not None
    _stub_subject("subject-replayed")

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={
            OAUTH_STATE_COOKIE: state,
            OAUTH_VERIFIER_COOKIE: verifier,
            HUMAN_SESSION_COOKIE: guest_b_token,
        },
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_missing_id_token(client: AsyncClient) -> None:
    state, verifier, _ = await _callback_cookies(client)

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"access_token": "access-only"}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_tampered_id_token(client: AsyncClient) -> None:
    state, verifier, nonce = await _callback_cookies(client)
    token = _signed_id_token(nonce=nonce)
    header, payload, signature = token.split(".")
    bad_signature = f"{'a' if signature[0] != 'a' else 'b'}{signature[1:]}"
    tampered = f"{header}.{payload}.{bad_signature}"

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": tampered}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("aud", "wrong-client"),
        ("iss", "https://evil.example"),
        ("nonce", "wrong-nonce"),
    ],
)
async def test_callback_rejects_id_token_claim_mismatch(
    client: AsyncClient, override: str, value: str
) -> None:
    state, verifier, nonce = await _callback_cookies(client)
    claims: dict[str, Any] = {"nonce": nonce}
    claims[override] = value

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": _signed_id_token(**claims)}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_id_token_without_exp(client: AsyncClient) -> None:
    """An id_token with no bounded lifetime is rejected fail-closed (US-193)."""
    state, verifier, nonce = await _callback_cookies(client)

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": _signed_id_token(nonce=nonce, include_exp=False)}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_expired_id_token(client: AsyncClient) -> None:
    """An expired (past exp) id_token is rejected (US-193)."""
    state, verifier, nonce = await _callback_cookies(client)

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": _signed_id_token(nonce=nonce, exp=int(time()) - 3600)}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


def test_state_signing_key_is_not_derivable_from_client_secret() -> None:
    """The state key must not be reconstructable from the client secret (US-201).

    The earlier fallback derived the key as
    ``base64url(HMAC(client_secret, domain-string))`` — fully reproducible by an
    attacker holding only the client secret. Assert the resolved key is NOT that
    derivation (and not the client secret), i.e. it is a genuinely independent
    server secret.
    """
    import base64
    import hashlib
    import hmac

    from padrino.api.oauth import resolve_oauth_config

    settings = get_settings()
    config = resolve_oauth_config(settings, "google")
    assert config is not None
    assert config.state_signing_key
    assert config.state_signing_key != config.client_secret
    derived = (
        base64.urlsafe_b64encode(
            hmac.new(
                config.client_secret.encode("utf-8"),
                b"padrino.oauth.state-signing-key.v1",
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert config.state_signing_key != derived


@pytest.mark.asyncio
async def test_callback_rejects_replayed_state_and_code(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Replaying the same (state cookie, code) yields one session + a rejection (US-202).

    The first callback succeeds and mints exactly one account session; a second
    callback with the IDENTICAL state cookie and code is rejected fail-closed
    (``oauth_state_replayed``) by the server-side single-use store, independent of
    whether the provider would have re-redeemed the code.
    """
    state, verifier, nonce = await _callback_cookies(client)
    _stub_valid_token(nonce, subject="subject-replay")
    cookies = {OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier}
    params = {"code": "auth-code", "state": state}

    first = await client.get("/human/oauth/google/callback", params=params, cookies=cookies)
    assert first.status_code == 200
    # The first login persisted a session cookie on the client jar; clear it so
    # the replayed request carries the IDENTICAL session-binding context as the
    # original (an attacker replaying the exact same request), isolating the
    # single-use store as the thing that rejects the replay.
    client.cookies.clear()

    second = await client.get("/human/oauth/google/callback", params=params, cookies=cookies)
    assert second.status_code == 400
    assert second.json()["detail"] == "oauth_state_replayed"

    async with session_factory() as session:
        principals = (await session.execute(select(Principal))).scalars().all()
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
        flows = (await session.execute(select(OAuthConsumedFlow))).scalars().all()
    # Exactly one successful session/account, and exactly one consumed-flow row.
    assert len(principals) == 1
    assert len(identities) == 1
    assert len(flows) == 1


@pytest.mark.asyncio
async def test_distinct_flows_each_consume_independently(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two distinct start->callback flows both succeed (single-use is per-flow).

    A fresh ``/start`` mints a new per-flow token, so a second legitimate sign-in
    is never blocked by the single-use store (only an exact replay of the same
    flow is rejected).
    """

    async def _login(subject: str) -> int:
        state, verifier, nonce = await _callback_cookies(client)
        _stub_valid_token(nonce, subject=subject)
        resp = await client.get(
            "/human/oauth/google/callback",
            params={"code": "auth-code", "state": state},
            cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
        )
        return resp.status_code

    assert await _login("subject-flow") == 200
    assert await _login("subject-flow") == 200

    async with session_factory() as session:
        flows = (await session.execute(select(OAuthConsumedFlow))).scalars().all()
    # Same account (repeat sign-in) but two distinct flows were consumed.
    assert len(flows) == 2


@pytest.mark.asyncio
async def test_try_consume_flow_is_single_use(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The repo claim is atomic: the first call wins, every later call loses."""
    from datetime import UTC, datetime, timedelta

    from padrino.db.repositories import oauth_consumed_flows as flows_repo

    now = datetime.now(UTC)
    async with session_factory() as session:
        assert await flows_repo.try_consume_flow(session, flow="flow-1", consumed_at=now) is True
        assert await flows_repo.try_consume_flow(session, flow="flow-1", consumed_at=now) is False
        # A distinct flow is still claimable.
        assert await flows_repo.try_consume_flow(session, flow="flow-2", consumed_at=now) is True
        await session.commit()

    async with session_factory() as session:
        # A replay across a fresh session/transaction is still rejected.
        assert await flows_repo.try_consume_flow(session, flow="flow-1", consumed_at=now) is False
        # Pruning past the TTL removes stale rows but is order-insensitive here.
        removed = await flows_repo.prune_expired(session, older_than=now + timedelta(seconds=1))
        assert removed == 2
        await session.commit()


def test_explicit_state_signing_key_used_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit server signing key is used verbatim for the state HMAC (US-201)."""
    from padrino.api.oauth import resolve_oauth_config

    monkeypatch.setenv("PADRINO_OAUTH_STATE_SIGNING_KEY", "dedicated-server-key")
    get_settings.cache_clear()
    try:
        config = resolve_oauth_config(get_settings(), "google")
        assert config is not None
        assert config.state_signing_key == "dedicated-server-key"
        assert config.state_signing_key != config.client_secret
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_state_signing_key_yields_no_oauth_config(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """An unset/blank state-signing key is fail-closed: no usable config (US-201)."""
    from padrino.api.oauth import resolve_oauth_config

    if value is None:
        monkeypatch.delenv("PADRINO_OAUTH_STATE_SIGNING_KEY", raising=False)
    else:
        monkeypatch.setenv("PADRINO_OAUTH_STATE_SIGNING_KEY", value)
    get_settings.cache_clear()
    try:
        assert resolve_oauth_config(get_settings(), "google") is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_routes_503_without_state_signing_key(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no dedicated state key the OAuth routes 503 (fail-closed, US-201)."""
    monkeypatch.delenv("PADRINO_OAUTH_STATE_SIGNING_KEY", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app(session_factory=session_factory, auth_required=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            start = await ac.get("/human/oauth/google/start")
            assert start.status_code == 503
            cb = await ac.get("/human/oauth/google/callback", params={"code": "c", "state": "s"})
            assert cb.status_code == 503
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_callback_rejects_multi_audience_with_foreign_azp(
    client: AsyncClient,
) -> None:
    """A token minted for another RP (aud=[us, other], azp=other) is rejected (US-201)."""
    state, verifier, nonce = await _callback_cookies(client)

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {
            "id_token": _signed_id_token(
                nonce=nonce,
                aud=["client-id-123", "other-rp"],
                azp="other-rp",
            )
        }

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_accepts_multi_audience_with_matching_azp(
    client: AsyncClient,
) -> None:
    """A multi-aud token whose azp is THIS client is accepted (US-201)."""
    state, verifier, nonce = await _callback_cookies(client)

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {
            "id_token": _signed_id_token(
                nonce=nonce,
                aud=["client-id-123", "other-rp"],
                azp="client-id-123",
            )
        }

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_callback_rejects_stale_but_unexpired_id_token(
    client: AsyncClient,
) -> None:
    """A stale-iat token with a far-future exp is rejected by the max-age ceiling."""
    state, verifier, nonce = await _callback_cookies(client)

    issued_at = int(time())
    # iat is well beyond the max-age ceiling, but exp is far in the future so
    # authlib's exp check alone would accept it.
    stale_iat = issued_at - 4000
    far_future_exp = issued_at + 10**6

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {"id_token": _signed_id_token(nonce=nonce, iat=stale_iat, exp=far_future_exp)}

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)
    # Pin the clock just after issuance for the authlib exp/iat checks, then the
    # max-age check measures the (large) gap to the stale iat.
    set_clock(lambda: float(issued_at))

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_accepts_id_token_within_default_max_age(
    client: AsyncClient,
) -> None:
    """The default ceiling allows a legitimately slow OAuth round trip (US-207)."""
    state, verifier, nonce = await _callback_cookies(client)

    issued_at = int(time())
    slow_but_valid_iat = issued_at - 800
    far_future_exp = issued_at + 10**6

    async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
        return {
            "id_token": _signed_id_token(
                nonce=nonce,
                iat=slow_but_valid_iat,
                exp=far_future_exp,
            )
        }

    async def _fetch(config: object) -> dict[str, object]:
        return _jwks()

    set_exchange_token(_exchange)
    set_fetch_jwks(_fetch)
    set_clock(lambda: float(issued_at))

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_callback_honors_configured_token_age_ceiling(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operators can tighten the defense-in-depth max-age ceiling (US-207)."""
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = _oauth_settings_with_max_token_age(120)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        state, verifier, nonce = await _callback_cookies(ac)

        issued_at = int(time())
        too_old_for_override_iat = issued_at - 300
        far_future_exp = issued_at + 10**6

        async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
            return {
                "id_token": _signed_id_token(
                    nonce=nonce,
                    iat=too_old_for_override_iat,
                    exp=far_future_exp,
                )
            }

        async def _fetch(config: object) -> dict[str, object]:
            return _jwks()

        set_exchange_token(_exchange)
        set_fetch_jwks(_fetch)
        set_clock(lambda: float(issued_at))

        resp = await ac.get(
            "/human/oauth/google/callback",
            params={"code": "auth-code", "state": state},
            cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_can_disable_extra_token_age_ceiling(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """None disables only the extra iat max-age check; exp still bounds the token."""
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = _oauth_settings_with_max_token_age(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        state, verifier, nonce = await _callback_cookies(ac)

        issued_at = int(time())
        old_but_unexpired_iat = issued_at - 4000
        far_future_exp = issued_at + 10**6

        async def _exchange(config: object, *, code: str, code_verifier: str) -> dict[str, str]:
            return {
                "id_token": _signed_id_token(
                    nonce=nonce,
                    iat=old_but_unexpired_iat,
                    exp=far_future_exp,
                )
            }

        async def _fetch(config: object) -> dict[str, object]:
            return _jwks()

        set_exchange_token(_exchange)
        set_fetch_jwks(_fetch)
        set_clock(lambda: float(issued_at))

        resp = await ac.get(
            "/human/oauth/google/callback",
            params={"code": "auth-code", "state": state},
            cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_expired_guest_session_is_not_upgraded(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import UTC, datetime, timedelta

    raw_token = "expired-guest-token"
    now = datetime.now(UTC)
    async with session_factory() as session:
        guest = await principals_repo.create_principal(
            session, kind=principals_repo.PRINCIPAL_KIND_GUEST
        )
        await principals_repo.create_session(
            session,
            principal_id=guest.id,
            raw_token=raw_token,
            kind=principals_repo.SESSION_KIND_GUEST,
            issued_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        await session.commit()
        guest_id = guest.id

    state, verifier, _ = await _callback_cookies(client, human_session=raw_token)
    _stub_subject("subject-expired-guest")

    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={
            OAUTH_STATE_COOKIE: state,
            OAUTH_VERIFIER_COOKIE: verifier,
            HUMAN_SESSION_COOKIE: raw_token,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["principal_id"] != str(guest_id)
    async with session_factory() as session:
        guest_after = await session.get(Principal, guest_id)
        principals = (await session.execute(select(Principal))).scalars().all()
    assert guest_after is not None
    assert guest_after.kind == "guest"
    assert len(principals) == 2


@pytest.mark.asyncio
async def test_routes_503_without_provider_config(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADRINO_OAUTH_PROVIDER", raising=False)
    monkeypatch.delenv("PADRINO_OAUTH_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app(
            session_factory=session_factory,
            auth_required=True,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            start = await ac.get("/human/oauth/google/start")
            assert start.status_code == 503
            cb = await ac.get("/human/oauth/google/callback", params={"code": "c", "state": "s"})
            assert cb.status_code == 503
    finally:
        get_settings.cache_clear()
