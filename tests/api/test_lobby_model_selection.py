"""US-348 canonical-slug lobby AI seat selection coverage."""

from __future__ import annotations

import asyncio
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
from padrino.api.lobby_launch import LobbyNotLaunchableError
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    Lobby,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
    PromptVersion,
    ProviderCredential,
)
from padrino.db.repositories import promotional_credit_ledger
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    create_or_get_completed_snapshot,
)
from padrino.settings import Settings

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


async def _grant_promotional_credit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    token: str,
) -> uuid.UUID:
    """Seed the existing integer-micro wallet with an isolated test grant."""
    me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: token})
    assert me.status_code == 200
    principal_id = uuid.UUID(me.json()["principal_id"])
    async with session_factory() as session, session.begin():
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal_id,
            reservation_id=None,
            game_id=None,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
            amount_micros=5_000_000,
            idempotency_key=f"test-grant:{principal_id}",
            reason="test_funding_quote",
            now=NOW,
        )
    return principal_id


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
    await _grant_promotional_credit(client, session_factory, token=token)
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
    preflight = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["allowed"] is True
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={"funding_quote_hash": preflight.json()["funding_quote_hash"]},
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


@pytest.mark.asyncio
async def test_platform_launch_reserves_a_confirmed_frozen_funding_quote(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A funded launch holds the frozen quote before materializing its game."""
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="1" * 64,
        entries=[_entry("openai/funded")],
    )
    token = await _guest_token(client)
    await _grant_promotional_credit(client, session_factory, token=token)

    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/funded"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)

    preflight = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert preflight.status_code == 200, preflight.text
    quote = preflight.json()
    assert quote["allowed"] is True
    assert quote["funding_source"] == "PLATFORM"
    assert quote["reservation_micros"] > 0

    launched = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={"funding_quote_hash": quote["funding_quote_hash"]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert launched.status_code == 200, launched.text
    game_id = uuid.UUID(launched.json()["game_id"])
    retry = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={"funding_quote_hash": quote["funding_quote_hash"]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {
        "lobby_id": str(lobby_id),
        "game_id": str(game_id),
        "status": "LAUNCHED",
        "created": False,
    }

    async with session_factory() as session:
        reservation = await session.scalar(
            select(PromotionalCreditReservation).where(
                PromotionalCreditReservation.lobby_id == lobby_id
            )
        )
        assert reservation is not None
        holds = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry).where(
                        PromotionalCreditLedgerEntry.reservation_id == reservation.id,
                        PromotionalCreditLedgerEntry.entry_type == "RESERVATION",
                    )
                )
            ).scalars()
        )
    assert reservation.game_id == game_id
    assert reservation.status == "ACTIVE"
    assert reservation.reserved_micros == quote["reservation_micros"]
    assert reservation.completion_reserve_per_game_cap_micros == 500_000
    assert reservation.completion_reserve_unfunded_policy == "COERCE"
    assert len(holds) == 1
    assert holds[0].amount_micros == -quote["reservation_micros"]


@pytest.mark.asyncio
async def test_completion_reserve_breaker_denies_new_platform_game_not_existing_admissions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A depleted operator reserve blocks a new quote before game materialization."""
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = Settings(padrino_openrouter_completion_reserve_global_micros=0)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as funded_client:
        await _seed_openrouter_selection_inputs(
            session_factory,
            content_hash="7" * 64,
            entries=[_entry("openai/completion-breaker")],
        )
        token = await _guest_token(funded_client)
        await _grant_promotional_credit(funded_client, session_factory, token=token)
        create = await funded_client.post(
            "/lobbies",
            json={
                "built_in_ruleset_id": "mini7_v1",
                "human_seat_count": 1,
                "ai_model_selections": _duplicate_model_selections("openai/completion-breaker"),
            },
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert create.status_code == 201, create.text
        lobby_id = uuid.UUID(create.json()["id"])
        await _ready_and_lock(funded_client, lobby_id=lobby_id, token=token)
        preflight = await funded_client.post(
            f"/lobbies/{lobby_id}/funding-preflight",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["allowed"] is False
        assert preflight.json()["reason"] == "completion_reserve_breaker_open"
        launch = await funded_client.post(
            f"/lobbies/{lobby_id}/launch",
            json={"funding_quote_hash": preflight.json()["funding_quote_hash"]},
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert launch.status_code == 409
        assert launch.json()["detail"] == "completion_reserve_breaker_open"
    async with session_factory() as session:
        assert list((await session.execute(select(Game))).scalars()) == []


@pytest.mark.asyncio
async def test_changed_model_selection_invalidates_a_platform_funding_confirmation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A quote cannot reserve funds after any frozen model/catalog change."""
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="2" * 64,
        entries=[_entry("openai/quote-a"), _entry("openai/quote-b")],
    )
    token = await _guest_token(client)
    await _grant_promotional_credit(client, session_factory, token=token)
    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/quote-a"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    original = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert original.status_code == 200, original.text
    assert original.json()["allowed"] is True

    update = await client.patch(
        f"/lobbies/{lobby_id}/configuration",
        json={"ai_model_selections": _duplicate_model_selections("openai/quote-b")},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert update.status_code == 200, update.text
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)
    principal_id = await _grant_promotional_credit_principal(client, token)

    stale = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={"funding_quote_hash": original.json()["funding_quote_hash"]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "funding_quote_stale"
    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())
        reservations = list((await session.execute(select(PromotionalCreditReservation))).scalars())
        totals = await promotional_credit_ledger.get_credit_totals(
            session,
            principal_id=principal_id,
        )
    assert games == []
    assert reservations == []
    assert totals.available_micros == 5_000_000


async def _grant_promotional_credit_principal(client: AsyncClient, token: str) -> uuid.UUID:
    """Read the authenticated principal id without exposing it from a quote."""
    me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: token})
    assert me.status_code == 200
    return uuid.UUID(me.json()["principal_id"])


@pytest.mark.asyncio
async def test_platform_preflight_explains_unpriced_models_as_byok_required(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A platform quote fails closed instead of inventing a price for a model."""
    unpriced = _entry("openai/unpriced").model_copy(
        update={"pricing_strings": {}, "pricing_micros": {}}
    )
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="3" * 64,
        entries=[unpriced],
    )
    token = await _guest_token(client)
    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/unpriced"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)
    preflight = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["allowed"] is False
    assert preflight.json()["reason"] == "byok_required"
    assert "unpriceable_model" in preflight.json()["reasons"]
    assert "byok_required" in preflight.json()["reasons"]


@pytest.mark.asyncio
async def test_byok_launch_requires_and_binds_an_explicit_maximum_without_promo_hold(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BYOK confirms a bound maximum but cannot reserve or debit promo credit."""
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="4" * 64,
        entries=[_entry("openai/byok-confirmed")],
    )
    token = await _guest_token(client)
    principal_id = await _grant_promotional_credit_principal(client, token)
    async with session_factory() as session, session.begin():
        session.add(
            ProviderCredential(
                principal_id=principal_id,
                provider="openrouter",
                ciphertext=b"test-ciphertext",
                nonce=b"test-nonce",
                key_version="test-v1",
                display_suffix="firm",
                label="test key",
            )
        )

    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_byok_model_selections("openai/byok-confirmed"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)

    missing = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["allowed"] is False
    assert missing.json()["reason"] == "byok_maximum_confirmation_required"

    confirmed = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        json={"byok_max_estimate_micros": 5_000_000},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert confirmed.status_code == 200, confirmed.text
    quote = confirmed.json()
    assert quote["allowed"] is True
    assert quote["funding_source"] == "BYOK_OWNER"
    assert quote["reservation_micros"] is None
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={
            "funding_quote_hash": quote["funding_quote_hash"],
            "byok_max_estimate_micros": 5_000_000,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert launched.status_code == 200, launched.text
    async with session_factory() as session:
        reservations = list((await session.execute(select(PromotionalCreditReservation))).scalars())
        entries = list((await session.execute(select(PromotionalCreditLedgerEntry))).scalars())
    assert reservations == []
    assert entries == []


@pytest.mark.asyncio
async def test_concurrent_platform_launches_cannot_overspend_one_wallet(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One exact-balance principal can admit one of two concurrent tables."""
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="5" * 64,
        entries=[_entry("openai/race")],
    )
    token = await _guest_token(client)
    principal_id = await _grant_promotional_credit_principal(client, token)
    lobby_ids: list[uuid.UUID] = []
    for _ in range(2):
        create = await client.post(
            "/lobbies",
            json={
                "built_in_ruleset_id": "mini7_v1",
                "human_seat_count": 1,
                "ai_model_selections": _duplicate_model_selections("openai/race"),
            },
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert create.status_code == 201, create.text
        lobby_id = uuid.UUID(create.json()["id"])
        lobby_ids.append(lobby_id)
        await _ready_and_lock(client, lobby_id=lobby_id, token=token)

    preflights = [
        await client.post(
            f"/lobbies/{lobby_id}/funding-preflight",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        for lobby_id in lobby_ids
    ]
    assert all(response.status_code == 200 for response in preflights)
    reservation_micros = preflights[0].json()["reservation_micros"]
    assert isinstance(reservation_micros, int) and reservation_micros > 0
    async with session_factory() as session, session.begin():
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal_id,
            reservation_id=None,
            game_id=None,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.GRANT,
            amount_micros=reservation_micros,
            idempotency_key=f"race-grant:{principal_id}",
            reason="test_exact_wallet_race",
            now=NOW,
        )

    launches = await asyncio.gather(
        *(
            client.post(
                f"/lobbies/{lobby_id}/launch",
                json={"funding_quote_hash": quote.json()["funding_quote_hash"]},
                cookies={HUMAN_SESSION_COOKIE: token},
            )
            for lobby_id, quote in zip(lobby_ids, preflights, strict=True)
        )
    )
    assert sorted(response.status_code for response in launches) == [200, 409]
    assert {
        response.json().get("detail") for response in launches if response.status_code == 409
    } == {"insufficient_promotional_credit"}
    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())
        reservations = list((await session.execute(select(PromotionalCreditReservation))).scalars())
        totals = await promotional_credit_ledger.get_credit_totals(
            session,
            principal_id=principal_id,
        )
    assert len(games) == 1
    assert len(reservations) == 1
    assert reservations[0].reserved_micros == reservation_micros
    assert totals.available_micros == 0


@pytest.mark.asyncio
async def test_failed_post_reservation_launch_leaves_no_promotional_hold(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A materialization failure cannot strand an active quote reservation."""
    await _seed_openrouter_selection_inputs(
        session_factory,
        content_hash="6" * 64,
        entries=[_entry("openai/rollback")],
    )
    token = await _guest_token(client)
    principal_id = await _grant_promotional_credit(client, session_factory, token=token)
    create = await client.post(
        "/lobbies",
        json={
            "built_in_ruleset_id": "mini7_v1",
            "human_seat_count": 1,
            "ai_model_selections": _duplicate_model_selections("openai/rollback"),
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert create.status_code == 201, create.text
    lobby_id = uuid.UUID(create.json()["id"])
    await _ready_and_lock(client, lobby_id=lobby_id, token=token)
    preflight = await client.post(
        f"/lobbies/{lobby_id}/funding-preflight",
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert preflight.status_code == 200, preflight.text

    async def _fail_after_reservation(*_args: object, **_kwargs: object) -> object:
        raise LobbyNotLaunchableError("test_game_materialization_failed")

    monkeypatch.setattr("padrino.api.lobby_launch.games_repo.create", _fail_after_reservation)
    launch = await client.post(
        f"/lobbies/{lobby_id}/launch",
        json={"funding_quote_hash": preflight.json()["funding_quote_hash"]},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert launch.status_code == 409
    assert launch.json()["detail"] == "test_game_materialization_failed"
    async with session_factory() as session:
        reservations = list((await session.execute(select(PromotionalCreditReservation))).scalars())
        entries = list((await session.execute(select(PromotionalCreditLedgerEntry))).scalars())
        totals = await promotional_credit_ledger.get_credit_totals(
            session,
            principal_id=principal_id,
        )
    assert reservations == []
    assert [entry.entry_type for entry in entries] == ["GRANT"]
    assert totals.available_micros == 5_000_000
