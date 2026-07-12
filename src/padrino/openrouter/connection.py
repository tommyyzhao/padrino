"""OpenRouter user-key connection flow outside the deterministic core.

This module implements the server side of OpenRouter's PKCE hand-off.  It
creates signed state bound to an authenticated Padrino session, exchanges the
short-lived OpenRouter authorization code over HTTPS, and validates the
returned user-controlled key before the caller encrypts it.  Plaintext keys
remain short-lived local variables and are deliberately absent from every
diagnostic type in this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from padrino.llm.secrets import SecretResolutionError, resolve_secret
from padrino.settings import Settings

OPENROUTER_PKCE_VERIFIER_COOKIE: Final[str] = "padrino_openrouter_pkce_verifier"
OPENROUTER_CONNECT_CALLBACK_PATH: Final[str] = "/human/openrouter/connect/callback"
OPENROUTER_PKCE_METHOD: Final[str] = "S256"
_STATE_VERSION: Final[int] = 1
_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class OpenRouterConnectionError(RuntimeError):
    """A connection-flow failure with a safe, stable reason token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, repr=False)
class OpenRouterConnectionConfig:
    """Resolved, deployment-owned configuration for the connection flow."""

    authorize_url: str
    exchange_url: str
    current_key_url: str
    callback_url: str
    state_signing_key: str = field(repr=False)
    timeout_seconds: float
    management_key_ref: str | None = field(repr=False)
    flow_ttl_seconds: int


@dataclass(frozen=True, slots=True, repr=False)
class OpenRouterConnectionStart:
    """Browser redirect plus ephemeral PKCE material for one connection flow."""

    authorization_url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    state_hash: str
    verifier_binding_hash: str


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedOpenRouterKey:
    """Safe metadata plus an unpersisted plaintext key from OpenRouter."""

    api_key: str = field(repr=False)
    display_suffix: str
    label: str | None
    upstream_key_hash: str | None
    credit_capability: str


def resolve_openrouter_connection_config(settings: Settings) -> OpenRouterConnectionConfig | None:
    """Return complete, exact callback-bound connection configuration.

    The callback URL is deployment-owned and must name only the fixed Padrino
    callback route.  The browser never supplies a callback target, eliminating
    open-redirect and code-exfiltration variants of the PKCE flow.
    """
    callback_url = settings.padrino_openrouter_connect_callback_url
    state_key = settings.padrino_openrouter_connect_state_signing_key
    if callback_url is None or state_key is None:
        return None
    signing_key = state_key.get_secret_value().strip()
    if not signing_key or not _is_exact_callback_url(callback_url):
        return None
    parsed_base = urlsplit(settings.padrino_openrouter_base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        return None
    origin = urlunsplit((parsed_base.scheme, parsed_base.netloc, "", "", ""))
    api_base = settings.padrino_openrouter_base_url.rstrip("/")
    return OpenRouterConnectionConfig(
        authorize_url=f"{origin}/auth",
        exchange_url=f"{api_base}/auth/keys",
        current_key_url=f"{api_base}/key",
        callback_url=callback_url,
        state_signing_key=signing_key,
        timeout_seconds=settings.padrino_openrouter_timeout_seconds,
        management_key_ref=settings.padrino_openrouter_management_key_ref,
        flow_ttl_seconds=settings.padrino_openrouter_connect_flow_ttl_seconds,
    )


def start_openrouter_connection(
    config: OpenRouterConnectionConfig,
    *,
    principal_id: uuid.UUID,
    session_id: uuid.UUID,
) -> OpenRouterConnectionStart:
    """Create an S256 authorization redirect bound to one Padrino session."""
    verifier = secrets.token_urlsafe(48)
    state = _encode_state(
        config,
        principal_id=principal_id,
        session_id=session_id,
        flow=secrets.token_urlsafe(32),
    )
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urlencode(
        {
            "callback_url": config.callback_url,
            "code_challenge": challenge,
            "code_challenge_method": OPENROUTER_PKCE_METHOD,
            "state": state,
        }
    )
    return OpenRouterConnectionStart(
        authorization_url=f"{config.authorize_url}?{query}",
        state=state,
        code_verifier=verifier,
        state_hash=hash_state(state),
        verifier_binding_hash=hash_verifier_binding(state, verifier),
    )


def validate_openrouter_connection_state(
    config: OpenRouterConnectionConfig,
    *,
    state: str,
    principal_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    """Fail closed unless signed state belongs to this principal and session."""
    try:
        payload = _decode_state(config, state)
        claimed_principal_id = payload.get("principal_id")
        claimed_session_id = payload.get("session_id")
        flow = payload.get("flow")
        valid = (
            payload.get("v") == _STATE_VERSION
            and isinstance(claimed_principal_id, str)
            and hmac.compare_digest(claimed_principal_id, str(principal_id))
            and isinstance(claimed_session_id, str)
            and hmac.compare_digest(claimed_session_id, str(session_id))
            and isinstance(flow, str)
            and bool(flow)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise OpenRouterConnectionError("state_invalid")


def hash_state(state: str) -> str:
    """Return the database-safe digest for a signed browser state token."""
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def hash_verifier_binding(state: str, verifier: str) -> str:
    """Bind a cookie-held PKCE verifier to exactly one signed state token."""
    return hashlib.sha256(f"{state}:{verifier}".encode("ascii")).hexdigest()


class OpenRouterConnectionClient:
    """Small HTTPS client for code exchange, key validation, and revocation."""

    def __init__(self, *, client: httpx.AsyncClient, config: OpenRouterConnectionConfig) -> None:
        self._client = client
        self._config = config

    async def exchange_and_validate(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> ValidatedOpenRouterKey:
        """Exchange an authorization code then authenticate the issued key."""
        if not code.strip() or not code_verifier.strip():
            raise OpenRouterConnectionError("exchange_failed")
        try:
            response = await self._client.post(
                self._config.exchange_url,
                json={
                    "code": code,
                    "code_verifier": code_verifier,
                    "code_challenge_method": OPENROUTER_PKCE_METHOD,
                },
            )
        except httpx.HTTPError as exc:
            raise OpenRouterConnectionError("exchange_failed") from exc
        if response.status_code != 200:
            raise OpenRouterConnectionError("exchange_failed")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OpenRouterConnectionError("exchange_failed") from exc
        if not isinstance(payload, dict):
            raise OpenRouterConnectionError("exchange_failed")
        key = payload.get("key")
        if not isinstance(key, str) or len(key.strip()) < 8:
            raise OpenRouterConnectionError("exchange_failed")
        return await self.validate_key(api_key=key.strip())

    async def validate_key(self, *, api_key: str) -> ValidatedOpenRouterKey:
        """Validate one plaintext key without persisting provider response data."""
        try:
            response = await self._client.get(
                self._config.current_key_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError as exc:
            raise OpenRouterConnectionError("validation_failed") from exc
        if response.status_code != 200:
            raise OpenRouterConnectionError("validation_failed")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OpenRouterConnectionError("validation_failed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise OpenRouterConnectionError("validation_failed")
        data = payload["data"]
        label = _safe_display_label(data.get("label"), api_key=api_key)
        raw_hash = data.get("hash")
        upstream_key_hash = (
            raw_hash if isinstance(raw_hash, str) and _KEY_HASH_RE.fullmatch(raw_hash) else None
        )
        return ValidatedOpenRouterKey(
            api_key=api_key,
            display_suffix=api_key[-4:],
            label=label,
            upstream_key_hash=upstream_key_hash,
            credit_capability="openrouter_user_key",
        )

    async def best_effort_revoke(self, *, api_key: str) -> None:
        """Delete an upstream key only when a management credential is available.

        A local disconnect must never fail merely because OpenRouter's optional
        management API is unavailable.  Callers intentionally discard every
        error from this best-effort path after the local credential is erased.
        """
        if self._config.management_key_ref is None:
            return
        try:
            key_info = await self.validate_key(api_key=api_key)
            key_hash = key_info.upstream_key_hash
            if key_hash is None:
                return
            management_key = resolve_secret(self._config.management_key_ref)
            response = await self._client.delete(
                f"{self._config.current_key_url}s/{key_hash}",
                headers={"Authorization": f"Bearer {management_key}"},
            )
            if response.status_code not in {200, 204, 404}:
                return
        except (OpenRouterConnectionError, SecretResolutionError, httpx.HTTPError):
            return


def _is_exact_callback_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.path == OPENROUTER_CONNECT_CALLBACK_PATH
        and not parsed.query
        and not parsed.fragment
    )


def _encode_state(
    config: OpenRouterConnectionConfig,
    *,
    principal_id: uuid.UUID,
    session_id: uuid.UUID,
    flow: str,
) -> str:
    payload = _base64url(
        json.dumps(
            {
                "flow": flow,
                "principal_id": str(principal_id),
                "session_id": str(session_id),
                "v": _STATE_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = _base64url(
        hmac.new(
            config.state_signing_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest()
    )
    return f"{payload}.{signature}"


def _decode_state(config: OpenRouterConnectionConfig, state: str) -> dict[str, object]:
    encoded_payload, separator, encoded_signature = state.partition(".")
    if not separator or not encoded_payload or not encoded_signature:
        raise ValueError("invalid state")
    expected_signature = _base64url(
        hmac.new(
            config.state_signing_key.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(encoded_signature, expected_signature):
        raise ValueError("invalid state")
    payload = json.loads(_base64url_decode(encoded_payload).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid state")
    return payload


def _safe_display_label(value: object, *, api_key: str) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 100
        or api_key in normalized
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        return None
    return normalized


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "OPENROUTER_CONNECT_CALLBACK_PATH",
    "OPENROUTER_PKCE_METHOD",
    "OPENROUTER_PKCE_VERIFIER_COOKIE",
    "OpenRouterConnectionClient",
    "OpenRouterConnectionConfig",
    "OpenRouterConnectionError",
    "OpenRouterConnectionStart",
    "ValidatedOpenRouterKey",
    "hash_state",
    "hash_verifier_binding",
    "resolve_openrouter_connection_config",
    "start_openrouter_connection",
    "validate_openrouter_connection_state",
]
