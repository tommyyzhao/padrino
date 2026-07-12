"""Authenticated envelope encryption for player-owned OpenRouter credentials.

This module is deliberately an impure boundary.  It owns deployment key-ring
parsing, AES-GCM encryption, and transactional rewrapping of database-backed
credential envelopes; neither plaintext nor cryptography reaches
``padrino.core``.  Callers keep transaction ownership so an OAuth callback can
encrypt, validate, and persist one connection atomically.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import ProviderCredential

_AES_GCM_KEY_BYTES = 32
_AES_GCM_NONCE_BYTES = 12
_KEY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CredentialEncryptionUnavailable(RuntimeError):
    """Raised when the deployment credential-key configuration is unavailable."""


class CredentialAuthenticationError(RuntimeError):
    """Raised when a credential envelope cannot be authenticated safely."""


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    """One persistable AES-GCM envelope with redacted diagnostic rendering."""

    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    key_version: str

    def __repr__(self) -> str:
        """Never include encryption bytes in diagnostics or accidental logs."""
        return f"CredentialEnvelope(key_version={self.key_version!r}, ciphertext=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class CredentialKeyRing:
    """A versioned deployment AES-256-GCM key ring.

    The first key in the deployment setting is active for new encryption;
    following keys remain decrypt-only during an online rotation.  Key bytes
    are never exposed by repr or error messages.
    """

    active_version: str
    _keys: Mapping[str, bytes]

    @classmethod
    def parse(cls, configured_ring: str | None) -> CredentialKeyRing:
        """Parse ``version:base64url-key[, ...]`` without echoing secrets."""
        if configured_ring is None or not configured_ring.strip():
            raise CredentialEncryptionUnavailable("credential encryption is unavailable")

        parsed: dict[str, bytes] = {}
        for entry in configured_ring.split(","):
            version, separator, encoded_key = entry.strip().partition(":")
            if (
                not separator
                or not _KEY_VERSION_PATTERN.fullmatch(version)
                or not encoded_key
                or version in parsed
            ):
                raise CredentialEncryptionUnavailable("credential encryption is unavailable")
            key = _decode_aes_gcm_key(encoded_key)
            parsed[version] = key

        if not parsed:
            raise CredentialEncryptionUnavailable("credential encryption is unavailable")
        active_version = next(iter(parsed))
        return cls(active_version=active_version, _keys=MappingProxyType(parsed))

    def encryption_key(self) -> bytes:
        """Return only the active deployment key for a short-lived AES-GCM call."""
        return self._keys[self.active_version]

    def decryption_key(self, key_version: str) -> bytes:
        """Return a configured historical key without disclosing ring topology."""
        try:
            return self._keys[key_version]
        except KeyError as exc:
            raise CredentialAuthenticationError(
                "credential envelope could not be authenticated"
            ) from exc

    def __repr__(self) -> str:
        """Expose only safe version metadata in diagnostics."""
        return (
            "CredentialKeyRing("
            f"active_version={self.active_version!r}, configured_versions=<redacted>)"
        )


class CredentialEnvelopeService:
    """Encrypt and decrypt credentials with contextual AES-GCM authentication."""

    def __init__(self, key_ring: CredentialKeyRing) -> None:
        self.key_ring = key_ring

    def encrypt(
        self,
        plaintext: str,
        *,
        principal_id: uuid.UUID,
        provider: str,
        credential_id: uuid.UUID,
    ) -> CredentialEnvelope:
        """Return an envelope authenticated to one principal/provider/credential."""
        if not plaintext:
            raise ValueError("credential value must not be empty")
        nonce = os.urandom(_AES_GCM_NONCE_BYTES)
        ciphertext = AESGCM(self.key_ring.encryption_key()).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            _associated_data(
                principal_id=principal_id,
                provider=provider,
                credential_id=credential_id,
            ),
        )
        return CredentialEnvelope(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self.key_ring.active_version,
        )

    def decrypt(
        self,
        *,
        ciphertext: bytes,
        nonce: bytes,
        key_version: str,
        principal_id: uuid.UUID,
        provider: str,
        credential_id: uuid.UUID,
    ) -> str:
        """Authenticate and decrypt an envelope without surfacing secret material."""
        try:
            plaintext = AESGCM(self.key_ring.decryption_key(key_version)).decrypt(
                nonce,
                ciphertext,
                _associated_data(
                    principal_id=principal_id,
                    provider=provider,
                    credential_id=credential_id,
                ),
            )
            return plaintext.decode("utf-8")
        except (
            CredentialAuthenticationError,
            InvalidTag,
            UnicodeDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise CredentialAuthenticationError(
                "credential envelope could not be authenticated"
            ) from exc

    def decrypt_provider_credential(self, credential: ProviderCredential) -> str:
        """Decrypt one active ORM row using its persisted associated context."""
        if (
            credential.revoked_at is not None
            or credential.ciphertext is None
            or credential.nonce is None
        ):
            raise CredentialAuthenticationError("credential envelope could not be authenticated")
        return self.decrypt(
            ciphertext=credential.ciphertext,
            nonce=credential.nonce,
            key_version=credential.key_version,
            principal_id=credential.principal_id,
            provider=credential.provider,
            credential_id=credential.id,
        )


def credential_key_ring_from_settings(settings: object) -> CredentialKeyRing:
    """Build the key ring only where BYOK connection capability is needed."""
    configured = getattr(settings, "padrino_openrouter_credential_master_key_ring", None)
    if isinstance(configured, SecretStr):
        raw_ring = configured.get_secret_value()
    elif isinstance(configured, str):
        raw_ring = configured
    else:
        raw_ring = None
    return CredentialKeyRing.parse(raw_ring)


def credential_encryption_available(settings: object) -> bool:
    """Return whether OpenRouter connect/BYOK can safely be enabled for a deployment."""
    try:
        credential_key_ring_from_settings(settings)
    except CredentialEncryptionUnavailable:
        return False
    return True


async def rotate_provider_credential(
    session: AsyncSession,
    *,
    credential_id: uuid.UUID,
    service: CredentialEnvelopeService,
    rotated_at: datetime,
) -> bool:
    """Rewrap a credential with the active key while the caller owns a transaction.

    ``False`` means the row was absent, revoked, or already on the active key.
    A decrypt/authentication failure rolls back with the surrounding transaction
    and never writes a partial envelope.
    """
    credential = (
        await session.execute(
            select(ProviderCredential)
            .where(ProviderCredential.id == credential_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        credential is None
        or credential.revoked_at is not None
        or credential.ciphertext is None
        or credential.nonce is None
        or credential.key_version == service.key_ring.active_version
    ):
        return False

    plaintext = service.decrypt_provider_credential(credential)
    try:
        envelope = service.encrypt(
            plaintext,
            principal_id=credential.principal_id,
            provider=credential.provider,
            credential_id=credential.id,
        )
    finally:
        del plaintext

    credential.ciphertext = envelope.ciphertext
    credential.nonce = envelope.nonce
    credential.key_version = envelope.key_version
    credential.updated_at = rotated_at
    await session.flush()
    return True


def _decode_aes_gcm_key(encoded_key: str) -> bytes:
    """Decode exactly one base64url AES-256-GCM key without leaking it."""
    try:
        padded = encoded_key + "=" * (-len(encoded_key) % 4)
        key = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialEncryptionUnavailable("credential encryption is unavailable") from exc
    if len(key) != _AES_GCM_KEY_BYTES:
        raise CredentialEncryptionUnavailable("credential encryption is unavailable")
    return key


def _associated_data(
    *,
    principal_id: uuid.UUID,
    provider: str,
    credential_id: uuid.UUID,
) -> bytes:
    """Build a stable unambiguous AES-GCM associated-data context."""
    normalized_provider = provider.strip().casefold()
    if not normalized_provider:
        raise CredentialAuthenticationError("credential envelope could not be authenticated")
    return json.dumps(
        {
            "credential_id": str(credential_id),
            "principal_id": str(principal_id),
            "provider": normalized_provider,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "CredentialAuthenticationError",
    "CredentialEncryptionUnavailable",
    "CredentialEnvelope",
    "CredentialEnvelopeService",
    "CredentialKeyRing",
    "credential_encryption_available",
    "credential_key_ring_from_settings",
    "rotate_provider_credential",
]
