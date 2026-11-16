"""Optional management-key-backed OpenRouter promotional subkeys (US-359).

The management API is a defence-in-depth limiter around Padrino's local
integer-micro ledger.  It is deliberately not a source of funding authority:
if it is unavailable, callers can retain the existing server inference key
behind local reservation controls.  Management and inference credentials live
on purpose-specific construction paths; neither client can be repurposed for
the other's endpoint family.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.clock import as_utc as _as_utc
from padrino.db.models import OpenRouterPlatformSubkey
from padrino.db.repositories import openrouter_platform_subkeys as subkeys_repo
from padrino.llm.secrets import SecretResolutionError, resolve_secret
from padrino.openrouter.credentials import (
    CredentialAuthenticationError,
    CredentialEncryptionUnavailable,
    CredentialEnvelopeService,
    credential_encryption_available,
    credential_key_ring_from_settings,
)
from padrino.settings import Settings

MANAGEMENT_KEY_PURPOSE: Final[str] = "openrouter_management"
INFERENCE_KEY_PURPOSE: Final[str] = "openrouter_inference"
_SUBKEY_ENVELOPE_PROVIDER: Final[str] = "openrouter_platform_subkey"
_HASH_LENGTH: Final[int] = 64


class PlatformSubkeyManagementError(RuntimeError):
    """A safe stable token for an upstream management-plane failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PlatformSubkeyProvisionError(ValueError):
    """A safe local validation or idempotency failure."""


@dataclass(frozen=True, slots=True)
class PlatformSubkeyRequest:
    """Account-scoped, ledger-derived facts used to mint one subkey."""

    principal_id: uuid.UUID
    limit_micros: int
    expires_at: datetime
    allowed_models: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.limit_micros, bool) or not isinstance(self.limit_micros, int):
            raise PlatformSubkeyProvisionError("subkey_limit_must_be_integer")
        if self.limit_micros < 0:
            raise PlatformSubkeyProvisionError("subkey_limit_invalid")
        if self.expires_at.tzinfo is None:
            raise PlatformSubkeyProvisionError("subkey_expiry_must_be_timezone_aware")
        normalized = tuple(model.strip() for model in self.allowed_models)
        if any(not model for model in normalized) or tuple(sorted(set(normalized))) != normalized:
            raise PlatformSubkeyProvisionError("subkey_allowlist_invalid")
        object.__setattr__(self, "allowed_models", normalized)

    @property
    def request_hash(self) -> str:
        """Hash all durable intent facts without retaining a raw management token."""
        return hashlib.sha256(
            json.dumps(
                {
                    "allowed_models": self.allowed_models,
                    "expires_at": _utc_iso(self.expires_at),
                    "limit_micros": self.limit_micros,
                    "principal_id": str(self.principal_id),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class CreatedOpenRouterKey:
    """One-time upstream plaintext plus tightly bounded management metadata."""

    api_key: str = field(repr=False)
    upstream_key_hash: str
    label: str | None


@dataclass(frozen=True, slots=True)
class ListedOpenRouterKey:
    """A non-secret management-list projection used only for reconciliation."""

    upstream_key_hash: str
    key_name: str
    disabled: bool


@dataclass(frozen=True, slots=True, repr=False)
class PlatformSubkeyResult:
    """A secret-free local provisioning result."""

    id: uuid.UUID
    created: bool
    reconciled_orphan: bool
    status: str


@dataclass(frozen=True, slots=True)
class PlatformSubkeyHealth:
    """Safe operator/health facts, with no keys, references, hashes, or PII."""

    mode: str
    management_configured: bool
    management_available: bool
    inference_configured: bool
    inference_available: bool
    encryption_available: bool
    upstream_allowlist_enabled: bool


class OpenRouterManagementClient:
    """Narrow client that can call only the OpenRouter management ``/keys`` API."""

    __slots__ = ("_allowlist_field", "_base_url", "_client", "_management_key")

    purpose: Final[str] = MANAGEMENT_KEY_PURPOSE

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        management_key: str,
        allowlist_field: str | None,
    ) -> None:
        if not management_key.strip():
            raise PlatformSubkeyManagementError("subkey_management_unavailable")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._management_key = management_key
        self._allowlist_field = allowlist_field

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> OpenRouterManagementClient:
        """Resolve only the dedicated management reference for a ``/keys`` client."""
        reference = settings.padrino_openrouter_management_key_ref
        if reference is None:
            raise PlatformSubkeyManagementError("subkey_management_unavailable")
        try:
            management_key = resolve_secret(reference)
        except SecretResolutionError as exc:
            raise PlatformSubkeyManagementError("subkey_management_unavailable") from exc
        return cls(
            client=(
                client
                if client is not None
                else httpx.AsyncClient(timeout=settings.padrino_openrouter_timeout_seconds)
            ),
            base_url=settings.padrino_openrouter_base_url,
            management_key=management_key,
            allowlist_field=settings.padrino_openrouter_subkey_allowlist_field,
        )

    async def aclose(self) -> None:
        """Close the injected or internally-created HTTP client."""
        await self._client.aclose()

    async def create_key(
        self,
        *,
        key_name: str,
        limit_micros: int,
        expires_at: datetime,
        allowed_models: Sequence[str],
    ) -> CreatedOpenRouterKey:
        """Create one limited inference key; its plaintext is consumed immediately."""
        body: dict[str, object] = {
            "expires_at": _utc_iso(expires_at),
            "include_byok_in_limit": False,
            "limit": limit_micros / 1_000_000,
            "name": key_name,
        }
        if self._allowlist_field is not None:
            body[self._allowlist_field] = list(allowed_models)
        response = await self._request("POST", "/keys", json=body)
        payload = _data_object(response, reason="subkey_management_response_invalid")
        key = payload.get("key")
        key_hash = payload.get("hash")
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(key_hash, str)
            or not _valid_hash(key_hash)
        ):
            raise PlatformSubkeyManagementError("subkey_management_response_invalid")
        label = _safe_label(payload.get("label"), forbidden=(key, self._management_key))
        return CreatedOpenRouterKey(api_key=key, upstream_key_hash=key_hash, label=label)

    async def find_by_name(self, key_name: str) -> ListedOpenRouterKey | None:
        """Find an upstream key after a create response timeout, never its plaintext."""
        response = await self._request("GET", "/keys")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PlatformSubkeyManagementError("subkey_management_response_invalid") from exc
        values = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise PlatformSubkeyManagementError("subkey_management_response_invalid")
        for raw in values:
            if not isinstance(raw, dict) or raw.get("name") != key_name:
                continue
            key_hash = raw.get("hash")
            if not isinstance(key_hash, str) or not _valid_hash(key_hash):
                continue
            return ListedOpenRouterKey(
                upstream_key_hash=key_hash,
                key_name=key_name,
                disabled=bool(raw.get("disabled", False)),
            )
        return None

    async def delete_key(self, upstream_key_hash: str) -> bool:
        """Delete one known key; failures remain a caller's best-effort concern."""
        if not _valid_hash(upstream_key_hash):
            return False
        try:
            response = await self._request(
                "DELETE", f"/keys/{upstream_key_hash}", allow_not_found=True
            )
        except PlatformSubkeyManagementError:
            return False
        return response.status_code in {200, 204, 404}

    async def disable_key(self, upstream_key_hash: str) -> bool:
        """Disable a key before a later retry may delete it; no plaintext is needed."""
        if not _valid_hash(upstream_key_hash):
            return False
        try:
            response = await self._request(
                "PATCH", f"/keys/{upstream_key_hash}", json={"disabled": True}
            )
        except PlatformSubkeyManagementError:
            return False
        return response.status_code in {200, 204}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        """Perform a management-only request and collapse diagnostics to tokens."""
        if not path.startswith("/keys"):
            raise ValueError("management client may call only /keys endpoints")
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._management_key}"},
                json=json,
            )
        except httpx.TimeoutException as exc:
            raise PlatformSubkeyManagementError("subkey_management_timeout") from exc
        except httpx.HTTPError as exc:
            raise PlatformSubkeyManagementError("subkey_management_unavailable") from exc
        if response.status_code == 403:
            raise PlatformSubkeyManagementError("subkey_management_forbidden")
        if response.status_code == 429:
            raise PlatformSubkeyManagementError("subkey_management_throttled")
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            raise PlatformSubkeyManagementError("subkey_management_unavailable")
        return response


class OpenRouterPlatformSubkeyProvisioner:
    """Provision, rotate, and locally disable account-promotional subkeys."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        management_client: OpenRouterManagementClient,
        settings: Settings,
        now: Callable[[], datetime],
        key_name: Callable[[PlatformSubkeyRequest], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._management_client = management_client
        self._settings = settings
        self._now = now
        self._key_name = key_name or _default_key_name

    async def provision(
        self,
        request: PlatformSubkeyRequest,
        *,
        rotate: bool = False,
    ) -> PlatformSubkeyResult:
        """Return the idempotent active key, or create a fresh encrypted envelope."""
        now = _as_utc(self._now())
        async with self._session_factory() as session:
            active = await subkeys_repo.get_active(session, principal_id=request.principal_id)
            if active is not None and not rotate:
                if _matches(active, request) and _as_utc(active.expires_at) > now:
                    return PlatformSubkeyResult(
                        id=active.id,
                        created=False,
                        reconciled_orphan=False,
                        status=active.status,
                    )
                if _matches(active, request):
                    rotate = True
                else:
                    raise PlatformSubkeyProvisionError("subkey_request_conflict")

        key_name = self._key_name(request)
        created, reconciled_orphan = await self._create_or_reconcile(
            key_name=key_name,
            request=request,
        )
        try:
            service = CredentialEnvelopeService(credential_key_ring_from_settings(self._settings))
        except CredentialEncryptionUnavailable as exc:
            await self._management_client.delete_key(created.upstream_key_hash)
            raise PlatformSubkeyProvisionError("subkey_encryption_unavailable") from exc

        row_id = uuid.uuid4()
        envelope = service.encrypt(
            created.api_key,
            principal_id=request.principal_id,
            provider=_SUBKEY_ENVELOPE_PROVIDER,
            credential_id=row_id,
        )
        old_hash: str | None = None
        try:
            async with self._session_factory() as session, session.begin():
                active = await subkeys_repo.get_active(
                    session,
                    principal_id=request.principal_id,
                    lock=True,
                )
                if active is not None and not rotate:
                    if _matches(active, request) and _as_utc(active.expires_at) > now:
                        await self._management_client.delete_key(created.upstream_key_hash)
                        return PlatformSubkeyResult(
                            id=active.id,
                            created=False,
                            reconciled_orphan=reconciled_orphan,
                            status=active.status,
                        )
                    raise PlatformSubkeyProvisionError("subkey_request_conflict")
                if active is not None:
                    old_hash = active.upstream_key_hash
                    subkeys_repo.deactivate(active, status="REVOKED", now=now)
                session.add(
                    OpenRouterPlatformSubkey(
                        id=row_id,
                        principal_id=request.principal_id,
                        ciphertext=envelope.ciphertext,
                        nonce=envelope.nonce,
                        key_version=envelope.key_version,
                        upstream_key_hash=created.upstream_key_hash,
                        key_name=key_name,
                        display_suffix=created.api_key[-4:],
                        label=created.label,
                        limit_micros=request.limit_micros,
                        expires_at=_as_utc(request.expires_at),
                        allowed_models=list(request.allowed_models),
                        allowlist_enforced_upstream=(
                            self._settings.padrino_openrouter_subkey_allowlist_field is not None
                        ),
                        request_hash=request.request_hash,
                        status="ACTIVE",
                        provisioned_at=now,
                    )
                )
                await session.flush()
        except IntegrityError as exc:
            await self._management_client.delete_key(created.upstream_key_hash)
            raise PlatformSubkeyProvisionError("subkey_store_conflict") from exc
        finally:
            del created
        if old_hash is not None:
            await self._management_client.delete_key(old_hash)
        return PlatformSubkeyResult(
            id=row_id,
            created=True,
            reconciled_orphan=reconciled_orphan,
            status="ACTIVE",
        )

    async def disable_for_principal(self, principal_id: uuid.UUID) -> bool:
        """Locally disable first, then best-effort upstream-disable without settlement coupling."""
        now = _as_utc(self._now())
        upstream_hash: str | None = None
        async with self._session_factory() as session, session.begin():
            active = await subkeys_repo.get_active(session, principal_id=principal_id, lock=True)
            if active is None:
                return True
            upstream_hash = active.upstream_key_hash
            subkeys_repo.deactivate(active, status="DISABLED", now=now)
            await session.flush()
        assert upstream_hash is not None
        return await self._management_client.disable_key(upstream_hash)

    async def _create_or_reconcile(
        self,
        *,
        key_name: str,
        request: PlatformSubkeyRequest,
    ) -> tuple[CreatedOpenRouterKey, bool]:
        """Do one create; after a response timeout retire a discovered orphan then rotate."""
        try:
            return (
                await self._management_client.create_key(
                    key_name=key_name,
                    limit_micros=request.limit_micros,
                    expires_at=request.expires_at,
                    allowed_models=request.allowed_models,
                ),
                False,
            )
        except PlatformSubkeyManagementError as exc:
            if exc.reason != "subkey_management_timeout":
                raise
        orphan = await self._management_client.find_by_name(key_name)
        if orphan is None:
            raise PlatformSubkeyManagementError("subkey_management_timeout")
        if not orphan.disabled:
            await self._management_client.delete_key(orphan.upstream_key_hash)
        return (
            await self._management_client.create_key(
                key_name=key_name,
                limit_micros=request.limit_micros,
                expires_at=request.expires_at,
                allowed_models=request.allowed_models,
            ),
            True,
        )


def resolve_platform_inference_key(settings: Settings) -> str:
    """Resolve only the server inference reference, never the management reference."""
    reference = settings.padrino_openrouter_inference_key_ref
    if reference is None:
        raise PlatformSubkeyManagementError("platform_inference_unavailable")
    try:
        return resolve_secret(reference)
    except SecretResolutionError as exc:
        raise PlatformSubkeyManagementError("platform_inference_unavailable") from exc


async def resolve_active_platform_subkey(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    settings: Settings,
    now: datetime,
) -> str | None:
    """Return a short-lived encrypted subkey plaintext when a valid local row exists."""
    row = await subkeys_repo.get_active(session, principal_id=principal_id)
    if row is None or _as_utc(row.expires_at) <= _as_utc(now):
        return None
    if row.ciphertext is None or row.nonce is None:
        return None
    try:
        service = CredentialEnvelopeService(credential_key_ring_from_settings(settings))
        return service.decrypt(
            ciphertext=row.ciphertext,
            nonce=row.nonce,
            key_version=row.key_version,
            principal_id=row.principal_id,
            provider=_SUBKEY_ENVELOPE_PROVIDER,
            credential_id=row.id,
        )
    except (CredentialAuthenticationError, CredentialEncryptionUnavailable):
        return None


def platform_subkey_health(settings: Settings) -> PlatformSubkeyHealth:
    """Project configured capability/fallback state without resolving into output fields."""
    management_configured = settings.padrino_openrouter_management_key_ref is not None
    inference_configured = settings.padrino_openrouter_inference_key_ref is not None
    management_available = _reference_available(settings.padrino_openrouter_management_key_ref)
    inference_available = _reference_available(settings.padrino_openrouter_inference_key_ref)
    encryption_available = credential_encryption_available(settings)
    if management_available and encryption_available:
        mode = "account_scoped_subkeys"
    elif inference_available:
        mode = "server_inference_fallback"
    else:
        mode = "platform_inference_unavailable"
    return PlatformSubkeyHealth(
        mode=mode,
        management_configured=management_configured,
        management_available=management_available,
        inference_configured=inference_configured,
        inference_available=inference_available,
        encryption_available=encryption_available,
        upstream_allowlist_enabled=settings.padrino_openrouter_subkey_allowlist_field is not None,
    )


def _reference_available(reference: str | None) -> bool:
    if reference is None:
        return False
    try:
        resolve_secret(reference)
    except SecretResolutionError:
        return False
    return True


def _data_object(response: httpx.Response, *, reason: str) -> dict[str, object]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise PlatformSubkeyManagementError(reason) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PlatformSubkeyManagementError(reason)
    return data


def _safe_label(value: object, *, forbidden: Sequence[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 100
        or any(secret and secret in normalized for secret in forbidden)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    return normalized


def _valid_hash(value: str) -> bool:
    return len(value) == _HASH_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _matches(row: OpenRouterPlatformSubkey, request: PlatformSubkeyRequest) -> bool:
    return row.request_hash == request.request_hash


def _default_key_name(request: PlatformSubkeyRequest) -> str:
    """Use an opaque deterministic label so reconciliation does not expose an account id."""
    return f"padrino-promo-{request.request_hash[:24]}"


def _utc_iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "INFERENCE_KEY_PURPOSE",
    "MANAGEMENT_KEY_PURPOSE",
    "OpenRouterManagementClient",
    "OpenRouterPlatformSubkeyProvisioner",
    "PlatformSubkeyHealth",
    "PlatformSubkeyManagementError",
    "PlatformSubkeyProvisionError",
    "PlatformSubkeyRequest",
    "PlatformSubkeyResult",
    "platform_subkey_health",
    "resolve_active_platform_subkey",
    "resolve_platform_inference_key",
]
