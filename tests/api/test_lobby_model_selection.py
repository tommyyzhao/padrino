"""US-348 canonical-slug lobby AI seat selection coverage."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import (
    AgentBuild,
    GameSeat,
    Lobby,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    PromptVersion,
    ProviderCredential,
)
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
)

NOW = datetime(2026, 7, 12, 18, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _guest_token(client: AsyncClient) -> str:
    response = await client.post("/human/guest")
    assert response.status_code == 201
    return response.cookies[HUMAN_SESSION_COOKIE]


def _entry(slug: str, *, availability: str = "available") -> CatalogEntryInput:
    return CatalogEntryInput(
        canonical_slug=slug,
        display_name=slug,
        description="A safe catalog model.",
        modalities=("text",),
        context_length=128_000,
        pricing_strings={"prompt": "0.0000001", "completion": "0.0000002"},
        pricing_micros={"prompt_per_million": 100, "completion_per_million": 200},
        supported_parameters=("temperature", "top_p", "max_tokens"),
        provider_summary={"max_completion_tokens": 4_096},
        expires_at=None,
        raw_metadata={"status": availability},
    )


async def _seed_openrouter_selection_inputs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    content_hash: str,
    entries: list[CatalogEntryInput],
    fetched_at: datetime = NOW,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        provider = await session.scalar(
            select(ModelProvider).where(ModelProvider.name == "openrouter")
        )
        if provider is None:
            provider = ModelProvider(
                name="openrouter",
                base_url="https://openrouter.example.test/api/v1",
                auth_secret_ref="env:OPENROUTER_PRIVATE_TEST_KEY",
            )
            session.add(provider)
        prompt = await session.scalar(
            select(PromptVersion).where(PromptVersion.ruleset_id == "mini7_v1")
        )
        if prompt is None:
            session.add(
                PromptVersion(
                    ruleset_id="mini7_v1",
                    version="lobby-selection-v1",
                    system_prompt="system",
                    developer_prompt="developer",
                    response_schema={"type": "object", "additionalProperties": False},
                    prompt_hash="b" * 64,
                )
            )
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash=content_hash,
            fetched_at=fetched_at,
            source_etag=None,
            source_version="catalog-v1",
            entries=entries,
        )
        return snapshot.id


def _duplicate_model_selections(slug: str) -> list[dict[str, object]]:
    return [
        {
            "seat_index": index,
            "model_slug": slug,
            "inference_params": {"temperature": 0.25, "top_p": 0.9, "max_tokens": 512},
            "routing_preferences": {"only": ["openrouter"], "allow_fallbacks": False},
            "funding_source": "platform",
        }
        for index in range(1, 7)
    ]


def _duplicate_byok_model_selections(slug: str) -> list[dict[str, object]]:
    return [
        {**selection, "funding_source": "byok"} for selection in _duplicate_model_selections(slug)
    ]


async def _ready_and_lock(client: AsyncClient, *, lobby_id: uuid.UUID, token: str) -> None:
    ready = await client.post(
        f"/lobbies/{lobby_id}/ready",
        json={"ready": True},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert ready.status_code == 200, ready.text
    locked = await client.post(
        f"/lobbies/{lobby_id}/lock",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert locked.status_code == 200, locked.text


@pytest.mark.asyncio
async def test_lobby_uses_slug_selections_not_agent_build_uuids_and_freezes_safe_snapshot(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot_id = await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="a" * 64,
        entries=[_entry("openai/duplicate-friendly"), _entry("openai/reconfigured")],
    )
    token = await _guest_token(client)
    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/duplicate-friendly"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(LobbySeat)
                    .where(LobbySeat.lobby_id == lobby_id)
                    .order_by(LobbySeat.seat_index)
                )
            ).scalars()
        )
    snapshots = [row.model_selection for row in rows if row.model_selection is not None]
    assert len(snapshots) == 6
    assert {snapshot["canonical_slug"] for snapshot in snapshots} == {"openai/duplicate-friendly"}
    assert {snapshot["catalog_snapshot_id"] for snapshot in snapshots} == {str(snapshot_id)}
    assert all(snapshot["prompt_hash"] == "b" * 64 for snapshot in snapshots)
    assert all(snapshot["inference_params"]["max_tokens"] == 512 for snapshot in snapshots)
    assert all(
        snapshot["routing_preferences"]["allow_fallbacks"] is False for snapshot in snapshots
    )
    assert all(snapshot["funding_owner_ref"] == "platform" for snapshot in snapshots)
    rendered = json.dumps(snapshots, sort_keys=True)
    for forbidden in ("OPENROUTER_PRIVATE_TEST_KEY", "credential", "api_key", "authorization"):
        assert forbidden not in rendered

    update = await client.patch(
        f"/lobbies/{lobby_id}/configuration",
        json={"ai_model_selections": _duplicate_model_selections("openai/reconfigured")},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert update.status_code == 200, update.text
    async with session_factory() as session:
        updated = list(
            (
                await session.execute(
                    select(LobbySeat.model_selection)
                    .where(LobbySeat.lobby_id == lobby_id, LobbySeat.model_selection.is_not(None))
                    .order_by(LobbySeat.seat_index)
                )
            ).scalars()
        )
    assert {snapshot["canonical_slug"] for snapshot in updated} == {"openai/reconfigured"}

    rejected_uuid_contract = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "prepick_agent_build_ids": [str(uuid.uuid4())],
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert rejected_uuid_contract.status_code == 422


@pytest.mark.asyncio
async def test_locked_lobby_materializes_frozen_snapshot_despite_later_catalog_change(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    frozen_snapshot_id = await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="c" * 64,
        entries=[_entry("openai/frozen")],
    )
    token = await _guest_token(client)
    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/frozen"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)

    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="d" * 64,
        fetched_at=NOW + timedelta(minutes=1),
        entries=[_entry("openai/frozen", availability="disabled")],
    )
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert launched.status_code == 200, launched.text
    game_id = uuid.UUID(launched.json()["game_id"])

    async with session_factory() as session:
        lobby = await session.get(Lobby, lobby_id)
        assert lobby is not None
        selected = list(
            (
                await session.execute(
                    select(LobbySeat.model_selection).where(
                        LobbySeat.lobby_id == lobby_id, LobbySeat.model_selection.is_not(None)
                    )
                )
            ).scalars()
        )
        game_build_ids = list(
            (
                await session.execute(
                    select(GameSeat.agent_build_id).where(
                        GameSeat.game_id == game_id, GameSeat.agent_build_id.is_not(None)
                    )
                )
            ).scalars()
        )
        builds = list((await session.execute(select(AgentBuild))).scalars())
        configs = list((await session.execute(select(ModelConfig))).scalars())
    assert {selection["catalog_snapshot_id"] for selection in selected} == {str(frozen_snapshot_id)}
    assert len(set(game_build_ids)) == 1
    assert len(builds) == 1
    assert len(configs) == 1
    assert configs[0].catalog_snapshot_id == frozen_snapshot_id


@pytest.mark.asyncio
async def test_unavailable_catalog_choice_fails_before_lobby_lock(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="e" * 64,
        entries=[_entry("openai/disabled", availability="disabled")],
    )
    token = await _guest_token(client)
    response = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/disabled"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "model_unavailable_before_lock"
    async with session_factory() as session:
        assert list((await session.execute(select(Lobby))).scalars()) == []


@pytest.mark.asyncio
async def test_revoked_host_byok_credential_denies_lobby_launch(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="f" * 64,
        entries=[_entry("openai/byok-revoked")],
    )
    token = await _guest_token(client)
    host = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: token})
    assert host.status_code == 200
    host_principal_id = uuid.UUID(host.json()["principal_id"])
    async with session_factory() as session, session.begin():
        credential = ProviderCredential(
            principal_id=host_principal_id,
            provider="openrouter",
            ciphertext=b"encrypted-placeholder",
            nonce=b"nonce-placeholder",
            key_version="v1",
            display_suffix="oked",
            label="private",
        )
        session.add(credential)
        await session.flush()
        credential.revoked_at = NOW
        credential.ciphertext = None
        credential.nonce = None

    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_byok_model_selections("openai/byok-revoked"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)

    launch = await client.post(
        f"/lobbies/{lobby_id}/launch",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert launch.status_code == 409
    assert launch.json()["detail"] == "byok_credential_unavailable"
