"""Server-side OAuth code flow for optional account sign-in (US-129).

ONE provider (e.g. Google) via Authlib's :class:`AsyncOAuth2Client`. The
authorization-code flow is protected with a CSRF ``state`` and PKCE
(``code_challenge_method=S256``). The provider client id/secret and endpoint
urls come from :class:`padrino.settings.Settings` and are optional/None so the
engine boots and the test suite runs without them; the client secret is never
logged. No provider tokens are persisted beyond completing the exchange — only
the stable ``(provider, subject)`` identity is stored.

Tests stub the network entirely by overriding :data:`exchange_code` via the
``resolve_user_info`` indirection (``_RESOLVE_USER_INFO``), so no live provider
is contacted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client

from padrino.settings import Settings


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
    scope: str


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
    )
    if any(value is None for value in required):
        return None
    assert settings.padrino_oauth_client_id is not None
    assert settings.padrino_oauth_client_secret is not None
    assert settings.padrino_oauth_authorize_url is not None
    assert settings.padrino_oauth_token_url is not None
    assert settings.padrino_oauth_userinfo_url is not None
    assert settings.padrino_oauth_redirect_url is not None
    return OAuthConfig(
        provider=settings.padrino_oauth_provider,
        client_id=settings.padrino_oauth_client_id,
        client_secret=settings.padrino_oauth_client_secret,
        authorize_url=settings.padrino_oauth_authorize_url,
        token_url=settings.padrino_oauth_token_url,
        userinfo_url=settings.padrino_oauth_userinfo_url,
        redirect_url=settings.padrino_oauth_redirect_url,
        scope=settings.padrino_oauth_scope,
    )


def build_authorization_request(config: OAuthConfig) -> AuthorizationRequest:
    """Build the provider authorization URL with a fresh CSRF state + PKCE."""
    code_verifier = generate_token(48)
    client = _client_for(config)
    url, state = client.create_authorization_url(config.authorize_url, code_verifier=code_verifier)
    return AuthorizationRequest(url=url, state=state, code_verifier=code_verifier)


async def _default_resolve_user_info(
    config: OAuthConfig, *, code: str, code_verifier: str
) -> OAuthUserInfo:
    """Exchange the code for a token and fetch userinfo (live provider path)."""
    client = _client_for(config)
    await client.fetch_token(
        config.token_url,
        code=code,
        code_verifier=code_verifier,
    )
    resp = await client.get(config.userinfo_url)
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    await client.aclose()
    return _user_info_from_payload(payload)


def _user_info_from_payload(payload: dict[str, Any]) -> OAuthUserInfo:
    subject = payload.get("sub") or payload.get("id")
    if not subject:
        raise OAuthError("provider userinfo is missing a subject")
    display = payload.get("name") or payload.get("email")
    return OAuthUserInfo(subject=str(subject), display_name=str(display) if display else None)


# Indirection so tests can stub the entire network round-trip (no live provider).
ResolveUserInfo = Callable[..., Awaitable[OAuthUserInfo]]
_RESOLVE_USER_INFO: ResolveUserInfo = _default_resolve_user_info


def set_resolve_user_info(fn: ResolveUserInfo) -> None:
    """Override the code->userinfo resolver (test-only seam)."""
    global _RESOLVE_USER_INFO
    _RESOLVE_USER_INFO = fn


def reset_resolve_user_info() -> None:
    """Restore the live provider resolver."""
    global _RESOLVE_USER_INFO
    _RESOLVE_USER_INFO = _default_resolve_user_info


async def exchange_code(config: OAuthConfig, *, code: str, code_verifier: str) -> OAuthUserInfo:
    """Resolve the user identity for an authorization ``code``."""
    return await _RESOLVE_USER_INFO(config, code=code, code_verifier=code_verifier)


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


__all__ = [
    "AuthorizationRequest",
    "OAuthConfig",
    "OAuthError",
    "OAuthUserInfo",
    "build_authorization_request",
    "exchange_code",
    "reset_resolve_user_info",
    "resolve_oauth_config",
    "set_resolve_user_info",
]
