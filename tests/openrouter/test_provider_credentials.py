"""US-354 tests for OpenRouter credential encryption and rotation."""

from __future__ import annotations

import base64
import json
import logging
import traceback
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse

from padrino.db.models import Principal, ProviderCredential
from padrino.export.bundle import (
    AgentBuildInfo,
    EventEnvelope,
    GameBundle,
    GameSeatInfo,
    canonical_bundle_bytes,
)
from padrino.openrouter.credentials import (
    CredentialAuthenticationError,
    CredentialEncryptionUnavailable,
    CredentialEnvelopeService,
    CredentialKeyRing,
    credential_encryption_available,
    credential_key_ring_from_settings,
    rotate_provider_credential,
)
from padrino.settings import Settings

_SECRET = "sk-or-v1-sentinel-provider-secret-must-never-leak"
_NOW = datetime(2026, 7, 12, 20, 15, tzinfo=UTC)


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _key_ring(*versions: tuple[str, bytes]) -> str:
    return ",".join(f"{version}:{_encoded_key(byte)}" for version, byte in versions)


def test_missing_or_invalid_master_key_leaves_platform_and_mock_settings_usable() -> None:
    missing = Settings(_env_file=None)
    assert missing.padrino_openrouter_credential_master_key_ring is None
    assert missing.padrino_openrouter_inference_key_ref is None
    assert credential_encryption_available(missing) is False
    with pytest.raises(CredentialEncryptionUnavailable, match="unavailable"):
        credential_key_ring_from_settings(missing)

    invalid = Settings(
        _env_file=None,
        padrino_openrouter_credential_master_key_ring=SecretStr("v1:not-a-256-bit-base64url-key"),
    )
    assert credential_encryption_available(invalid) is False
    with pytest.raises(CredentialEncryptionUnavailable) as exc_info:
        credential_key_ring_from_settings(invalid)
    assert "not-a-256-bit-base64url-key" not in str(exc_info.value)


def test_settings_master_key_ring_is_secret_and_requires_versioned_high_entropy_keys() -> None:
    configured = _key_ring(("v2", b"b"), ("v1", b"a"))
    settings = Settings(
        _env_file=None,
        padrino_openrouter_credential_master_key_ring=SecretStr(configured),
    )

    assert isinstance(settings.padrino_openrouter_credential_master_key_ring, SecretStr)
    assert credential_encryption_available(settings) is True
    assert credential_key_ring_from_settings(settings).active_version == "v2"
    assert configured not in repr(settings)
    assert configured not in settings.model_dump_json()


def test_envelope_authenticates_credential_principal_and_provider_context() -> None:
    service = CredentialEnvelopeService(CredentialKeyRing.parse(_key_ring(("v1", b"a"))))
    principal_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    envelope = service.encrypt(
        _SECRET,
        principal_id=principal_id,
        provider="openrouter",
        credential_id=credential_id,
    )

    assert envelope.key_version == "v1"
    assert envelope.ciphertext != _SECRET.encode("utf-8")
    assert (
        service.decrypt(
            ciphertext=envelope.ciphertext,
            nonce=envelope.nonce,
            key_version=envelope.key_version,
            principal_id=principal_id,
            provider="openrouter",
            credential_id=credential_id,
        )
        == _SECRET
    )

    contexts = (
        (uuid.uuid4(), "openrouter", credential_id),
        (principal_id, "another-provider", credential_id),
        (principal_id, "openrouter", uuid.uuid4()),
    )
    for wrong_principal_id, wrong_provider, wrong_credential_id in contexts:
        with pytest.raises(CredentialAuthenticationError) as exc_info:
            service.decrypt(
                ciphertext=envelope.ciphertext,
                nonce=envelope.nonce,
                key_version=envelope.key_version,
                principal_id=wrong_principal_id,
                provider=wrong_provider,
                credential_id=wrong_credential_id,
            )
        assert _SECRET not in str(exc_info.value)


async def test_rotation_rewraps_old_key_version_inside_callers_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old_service = CredentialEnvelopeService(CredentialKeyRing.parse(_key_ring(("v1", b"a"))))
    rotating_service = CredentialEnvelopeService(
        CredentialKeyRing.parse(_key_ring(("v2", b"b"), ("v1", b"a")))
    )

    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        session.add(principal)
        await session.flush()
        credential = ProviderCredential(
            principal_id=principal.id,
            provider="openrouter",
            ciphertext=b"placeholder",
            nonce=b"placeholder",
            key_version="v1",
            display_suffix="1234",
            label="Personal OpenRouter",
        )
        session.add(credential)
        await session.flush()
        envelope = old_service.encrypt(
            _SECRET,
            principal_id=principal.id,
            provider=credential.provider,
            credential_id=credential.id,
        )
        credential.ciphertext = envelope.ciphertext
        credential.nonce = envelope.nonce
        credential_id = credential.id
        principal_id = principal.id

    async with session_factory() as session, session.begin():
        assert (
            await rotate_provider_credential(
                session,
                credential_id=credential_id,
                service=rotating_service,
                rotated_at=_NOW,
            )
            is True
        )

    async with session_factory() as session:
        rotated = await session.get(ProviderCredential, credential_id)
        assert rotated is not None
        assert rotated.key_version == "v2"
        assert rotated.updated_at.replace(tzinfo=UTC) == _NOW
        assert rotating_service.decrypt_provider_credential(rotated) == _SECRET
        with pytest.raises(CredentialAuthenticationError):
            old_service.decrypt_provider_credential(rotated)
        assert rotated.principal_id == principal_id


def test_secret_values_are_redacted_from_errors_repr_logs_db_repr_and_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = CredentialEnvelopeService(CredentialKeyRing.parse(_key_ring(("v1", b"a"))))
    principal_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    envelope = service.encrypt(
        _SECRET,
        principal_id=principal_id,
        provider="openrouter",
        credential_id=credential_id,
    )
    credential = ProviderCredential(
        id=credential_id,
        principal_id=principal_id,
        provider="openrouter",
        ciphertext=envelope.ciphertext,
        nonce=envelope.nonce,
        key_version=envelope.key_version,
        display_suffix="1234",
        label="Personal OpenRouter",
    )

    with pytest.raises(CredentialAuthenticationError) as exc_info:
        service.decrypt(
            ciphertext=envelope.ciphertext,
            nonce=envelope.nonce,
            key_version=envelope.key_version,
            principal_id=principal_id,
            provider="openrouter",
            credential_id=uuid.uuid4(),
        )

    with caplog.at_level(logging.WARNING):
        logging.getLogger("padrino.test.credentials").warning(
            "credential rejected: %s", exc_info.value
        )
    event = EventEnvelope(
        sequence=0,
        event_type="CredentialConnected",
        phase="SETUP",
        visibility="SYSTEM",
        actor_player_id=None,
        payload={"credential_id": str(credential.id), "key_version": credential.key_version},
        prev_event_hash="0" * 64,
        event_hash="a" * 64,
    )
    bundle = GameBundle(
        ruleset_id="mini7_v1",
        league_id=None,
        gauntlet_id=None,
        game_id="credential-export-game",
        seed="credential-export-seed",
        terminal_result=None,
        tip_hash="a" * 64,
        agent_builds=[
            AgentBuildInfo(
                public_player_id="P01",
                seat_index=0,
                display_name="OpenRouter",
                prompt_version="v1",
                model_provider="openrouter",
                model_name="safe-model",
                model_version=None,
            )
        ],
        game_seats=[
            GameSeatInfo(
                public_player_id="P01",
                seat_index=0,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
                death_phase=None,
            )
        ],
        events=[event],
    )
    status_response = JSONResponse(
        {
            "connected": True,
            "display_suffix": credential.display_suffix,
            "key_version": credential.key_version,
        }
    )
    safe_outputs = (
        repr(envelope),
        repr(service.key_ring),
        repr(credential),
        str(exc_info.value),
        "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)),
        caplog.text,
        event.model_dump_json(),
        canonical_bundle_bytes(bundle).decode("utf-8"),
        bytes(status_response.body).decode("utf-8"),
        json.dumps(
            {
                "credential_id": str(credential.id),
                "provider": credential.provider,
                "key_version": credential.key_version,
                "display_suffix": credential.display_suffix,
            },
            sort_keys=True,
        ),
    )
    assert all(_SECRET not in output for output in safe_outputs)
