"""Server-side OAuth code flow for optional account sign-in (US-129).

ONE provider (e.g. Google) via Authlib's :class:`AsyncOAuth2Client`. The
authorization-code flow is protected with a signed, session-bound CSRF
``state`` plus PKCE (``code_challenge_method=S256``); the state HMAC uses a
dedicated server signing key (US-193), not the provider client secret. The provider client
id/secret, endpoints, issuer, and JWKS URL come from
:class:`padrino.settings.Settings` and are optional/None so the engine boots and
the test suite runs without them; the client secret is never logged. No provider
tokens are persisted beyond completing the exchange — only the stable
``(provider, subject)`` identity is stored after validating the provider
``id_token`` signature, audience, issuer, nonce, and a bounded lifetime
(``exp``/``iat`` essential; a token with no ``exp`` is rejected fail-closed).

Tests stub the network entirely via the ``resolve_user_info`` indirection or the
lower-level token/JWKS seams, so no live provider is contacted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError

from padrino.settings import Settings

_STATE_VERSION: Final[int] = 1
# Small clock-skew tolerance for id_token exp/iat validation (US-193).
_CLAIMS_LEEWAY_SECONDS: Final[int] = 60


@dataclass(frozen=True)
class OAuthConfig:
    """Resolved, complete OAuth provider configuration (all fields present)."""

    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    redirect_url: str
    issuer: str
    jwks_url: str
    scope: str
    state_signing_key: str


@dataclass(frozen=True)
class OAuthUserInfo:
    """The identity extracted from a completed code exchange."""

    subject: str
    display_name: str | None


@dataclass(frozen=True)
class AuthorizationRequest:
    """An authorization URL plus the CSRF/PKCE secrets to carry to the callback."""

    url: str
    state: str
    code_verifier: str
    nonce: str


def resolve_oauth_config(settings: Settings, provider: str) -> OAuthConfig | None:
    """Return the provider config, or None if it is not fully configured.

    Returns None when the requested ``provider`` does not match the configured
    one or when any required credential/url is missing, so the routes can 503
    cleanly while the engine still boots without OAuth secrets.
    """
    if settings.padrino_oauth_provider is None:
        return None
    if provider != settings.padrino_oauth_provider:
        return None
    required = (
        settings.padrino_oauth_client_id,
        settings.padrino_oauth_client_secret,
        settings.padrino_oauth_authorize_url,
        settings.padrino_oauth_token_url,
        settings.padrino_oauth_userinfo_url,
        settings.padrino_oauth_redirect_url,
        settings.padrino_oauth_issuer,
        settings.padrino_oauth_jwks_url,
    )
    if any(value is None for value in required):
        return None
    assert settings.padrino_oauth_client_id is not None
    assert settings.padrino_oauth_client_secret is not None
    assert settings.padrino_oauth_authorize_url is not None
    assert settings.padrino_oauth_token_url is not None
    assert settings.padrino_oauth_userinfo_url is not None
    assert settings.padrino_oauth_redirect_url is not None
    assert settings.padrino_oauth_issuer is not None
    assert settings.padrino_oauth_jwks_url is not None
    return OAuthConfig(
        provider=settings.padrino_oauth_provider,
        client_id=settings.padrino_oauth_client_id,
        client_secret=settings.padrino_oauth_client_secret,
        authorize_url=settings.padrino_oauth_authorize_url,
        token_url=settings.padrino_oauth_token_url,
        userinfo_url=settings.padrino_oauth_userinfo_url,
        redirect_url=settings.padrino_oauth_redirect_url,
        issuer=settings.padrino_oauth_issuer,
        jwks_url=settings.padrino_oauth_jwks_url,
        scope=settings.padrino_oauth_scope,
        state_signing_key=_resolve_state_signing_key(settings),
    )


def _resolve_state_signing_key(settings: Settings) -> str:
    """Return the dedicated OAuth state-signing key (US-193).

    Uses the explicit server signing key when configured. Otherwise derives a
    key from the client secret via a domain-separated HMAC so the state HMAC is
    never keyed directly on ``client_secret`` (a leaked client secret cannot be
    used to forge state tokens without also knowing the derivation), while the
    flow still works with no extra deploy configuration.
    """
    explicit = settings.padrino_oauth_state_signing_key
    if explicit is not None and explicit.strip():
        return explicit
    secret = settings.padrino_oauth_client_secret or ""
    derived = hmac.new(
        secret.encode("utf-8"),
        b"padrino.oauth.state-signing-key.v1",
        hashlib.sha256,
    ).digest()
    return _base64url_encode(derived)


def build_authorization_request(
    config: OAuthConfig, *, session_binding: str = ""
) -> AuthorizationRequest:
    """Build the provider authorization URL with a fresh CSRF state + PKCE."""
    code_verifier = generate_token(48)
    nonce = generate_token(32)
    state = _encode_state(config, nonce=nonce, session_binding=session_binding)
    client = _client_for(config)
    url, state = client.create_authorization_url(
        config.authorize_url,
        state=state,
        code_verifier=code_verifier,
        nonce=nonce,
    )
    return AuthorizationRequest(url=url, state=state, code_verifier=code_verifier, nonce=nonce)


def oauth_session_binding(raw_session_token: str | None) -> str:
    """Return the stable binding value for the current human session cookie."""
    raw = (raw_session_token or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_authorization_state(
    config: OAuthConfig,
    *,
    received_state: str,
    expected_state: str,
    session_binding: str,
) -> str:
    """Validate the signed OAuth state and return its expected ID-token nonce."""
    if not hmac.compare_digest(received_state, expected_state):
        raise OAuthError("OAuth state mismatch")
    try:
        payload = _decode_state(config, received_state)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OAuthError("OAuth state is invalid") from exc
    if payload.get("v") != _STATE_VERSION:
        raise OAuthError("OAuth state version mismatch")
    nonce = payload.get("nonce")
    stored_binding = payload.get("session_binding")
    if not isinstance(nonce, str) or not nonce:
        raise OAuthError("OAuth state nonce missing")
    if not isinstance(stored_binding, str):
        raise OAuthError("OAuth state session binding missing")
    if not hmac.compare_digest(stored_binding, session_binding):
        raise OAuthError("OAuth state session mismatch")
    return nonce


async def _default_resolve_user_info(
    config: OAuthConfig, *, code: str, code_verifier: str, nonce: str
) -> OAuthUserInfo:
    """Exchange the code and validate the provider ID token (live path)."""
    try:
        token = await _EXCHANGE_TOKEN(config, code=code, code_verifier=code_verifier)
        jwks = await _FETCH_JWKS(config)
    except OAuthError:
        raise
    except Exception as exc:
        raise OAuthError("OAuth provider exchange failed") from exc
    id_token = token.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        raise OAuthError("OAuth provider token response is missing id_token")
    return _user_info_from_id_token(config, id_token=id_token, jwks=jwks, nonce=nonce)


async def _default_exchange_token(
    config: OAuthConfig, *, code: str, code_verifier: str
) -> dict[str, Any]:
    """Exchange an authorization code for a token set."""
    client = _client_for(config)
    try:
        token = await client.fetch_token(
            config.token_url,
            code=code,
            code_verifier=code_verifier,
        )
        return dict(token)
    finally:
        await client.aclose()


async def _default_fetch_jwks(config: OAuthConfig) -> dict[str, Any]:
    """Fetch the provider's JWKS document."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(config.jwks_url)
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, dict):
        raise OAuthError("OAuth provider JWKS response is invalid")
    return payload


def _user_info_from_id_token(
    config: OAuthConfig, *, id_token: str, jwks: dict[str, Any], nonce: str
) -> OAuthUserInfo:
    """Validate an OIDC ID token and extract the stable account identity."""
    try:
        key_set = JsonWebKey.import_key_set(jwks)
        claims = jwt.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "value": config.issuer},
                "aud": {"essential": True, "value": config.client_id},
                "sub": {"essential": True},
                "nonce": {"essential": True, "value": nonce},
                # A signature-valid token with no bounded lifetime must be
                # REJECTED fail-closed (US-193): mark ``exp`` essential so a
                # missing ``exp`` fails validation rather than being treated as a
                # permanent, non-expiring login assertion. ``iat`` is also
                # required so an issued-at can be sanity-checked.
                "exp": {"essential": True},
                "iat": {"essential": True},
            },
        )
        claims.validate(leeway=_CLAIMS_LEEWAY_SECONDS)
    except (JoseError, TypeError, ValueError) as exc:
        raise OAuthError("OAuth provider id_token validation failed") from exc
    return _user_info_from_payload(dict(claims))


def _user_info_from_payload(payload: dict[str, Any]) -> OAuthUserInfo:
    subject = payload.get("sub") or payload.get("id")
    if not subject:
        raise OAuthError("provider userinfo is missing a subject")
    display = payload.get("name") or payload.get("email")
    return OAuthUserInfo(subject=str(subject), display_name=str(display) if display else None)


# Indirection so tests can stub the entire network round-trip (no live provider).
ResolveUserInfo = Callable[..., Awaitable[OAuthUserInfo]]
_RESOLVE_USER_INFO: ResolveUserInfo = _default_resolve_user_info
ExchangeToken = Callable[..., Awaitable[dict[str, Any]]]
FetchJwks = Callable[..., Awaitable[dict[str, Any]]]
_EXCHANGE_TOKEN: ExchangeToken = _default_exchange_token
_FETCH_JWKS: FetchJwks = _default_fetch_jwks


def set_resolve_user_info(fn: ResolveUserInfo) -> None:
    """Override the code->userinfo resolver (test-only seam)."""
    global _RESOLVE_USER_INFO
    _RESOLVE_USER_INFO = fn


def reset_resolve_user_info() -> None:
    """Restore the live provider resolver."""
    global _RESOLVE_USER_INFO
    _RESOLVE_USER_INFO = _default_resolve_user_info


def set_exchange_token(fn: ExchangeToken) -> None:
    """Override the code->token-set exchange (test-only seam)."""
    global _EXCHANGE_TOKEN
    _EXCHANGE_TOKEN = fn


def set_fetch_jwks(fn: FetchJwks) -> None:
    """Override provider JWKS retrieval (test-only seam)."""
    global _FETCH_JWKS
    _FETCH_JWKS = fn


def reset_oauth_io() -> None:
    """Restore the live provider token/JWKS I/O helpers."""
    global _EXCHANGE_TOKEN, _FETCH_JWKS
    _EXCHANGE_TOKEN = _default_exchange_token
    _FETCH_JWKS = _default_fetch_jwks


async def exchange_code(
    config: OAuthConfig, *, code: str, code_verifier: str, nonce: str
) -> OAuthUserInfo:
    """Resolve the user identity for an authorization ``code``."""
    return await _RESOLVE_USER_INFO(config, code=code, code_verifier=code_verifier, nonce=nonce)


def _client_for(config: OAuthConfig) -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=config.redirect_url,
        scope=config.scope,
        code_challenge_method="S256",
    )


class OAuthError(Exception):
    """Raised when the OAuth exchange cannot produce a usable identity."""


def _encode_state(config: OAuthConfig, *, nonce: str, session_binding: str) -> str:
    payload = {
        "v": _STATE_VERSION,
        "flow": generate_token(32),
        "nonce": nonce,
        "session_binding": session_binding,
    }
    body = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _state_signature(config, body)
    return f"{body}.{signature}"


def _decode_state(config: OAuthConfig, state: str) -> dict[str, Any]:
    body, signature = state.split(".", 1)
    expected_signature = _state_signature(config, body)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("invalid OAuth state signature")
    decoded = _base64url_decode(body)
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid OAuth state payload")
    return payload


def _state_signature(config: OAuthConfig, body: str) -> str:
    digest = hmac.new(
        config.state_signing_key.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _base64url_encode(digest)


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


__all__ = [
    "AuthorizationRequest",
    "OAuthConfig",
    "OAuthError",
    "OAuthUserInfo",
    "build_authorization_request",
    "exchange_code",
    "oauth_session_binding",
    "reset_oauth_io",
    "reset_resolve_user_info",
    "resolve_oauth_config",
    "set_exchange_token",
    "set_fetch_jwks",
    "set_resolve_user_info",
    "validate_authorization_state",
]
