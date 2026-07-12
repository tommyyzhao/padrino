"""US-356 BYOK adapter assembly and durable call-attribution coverage."""

from __future__ import annotations

import asyncio
import base64
import uuid
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, LeagueKind, Role, SeatKind
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    League,
    LlmCall,
    Lobby,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
    ProviderCredential,
)
from padrino.economics.human_cost_governance import global_human_lane_spend_usd
from padrino.llm.adapter import LlmAdapter, OpenRouterProviderPreferences
from padrino.openrouter.byok_runtime import (
    ByokRuntimeError,
    resolve_byok_runtime_bindings,
    validate_byok_lobby_credentials,
)
from padrino.openrouter.credentials import CredentialEnvelopeService, CredentialKeyRing
from padrino.openrouter.lobby_model_selection import FrozenLobbyModelSelection
from padrino.runner.game_runner import GameConfig, GamePersistence, _persist_llm_call
from padrino.runner.human_lane import _run_one_human_game, build_human_lane_runtime
from padrino.settings import Settings

_HOST_KEY_A = "sk-or-host-a-sentinel-not-in-persistence"
_HOST_KEY_B = "sk-or-host-b-sentinel-not-in-persistence"
_INVITEE_KEY = "sk-or-invitee-sentinel-must-not-be-read"


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        padrino_openrouter_credential_master_key_ring=SecretStr(f"v1:{_encoded_key(b'a')}"),
        padrino_openrouter_base_url="https://openrouter.example.test/api/v1",
        padrino_openrouter_app_url="https://padrino.example.test",
        padrino_openrouter_app_title="Padrino BYOK test",
    )


def _observation(player_id: str) -> Observation:
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id="G-BYOK",
        phase="DAY_1_DISCUSSION_ROUND_1",
        day=1,
        round=1,
        you=YouInfo(
            player_id=player_id,
            alive=True,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
        ),
        alive_players=(player_id,),
        dead_players=(),
        public_events=(),
        private_events=(),
        legal_actions=LegalActions(allowed_action_types=[ActionType.NOOP], legal_targets=[]),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=500,
            private_message_max_chars=500,
            memory_update_max_chars=500,
        ),
    )


async def _seed_credential(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    secret: str,
) -> ProviderCredential:
    service = CredentialEnvelopeService(CredentialKeyRing.parse(f"v1:{_encoded_key(b'a')}"))
    credential = ProviderCredential(
        principal_id=principal_id,
        provider="openrouter",
        ciphertext=b"pending",
        nonce=b"pending",
        key_version="v1",
        display_suffix=secret[-4:],
        label="private key",
    )
    session.add(credential)
    await session.flush()
    envelope = service.encrypt(
        secret,
        principal_id=principal_id,
        provider=credential.provider,
        credential_id=credential.id,
    )
    credential.ciphertext = envelope.ciphertext
    credential.nonce = envelope.nonce
    return credential


def _selection(
    *,
    provider_id: uuid.UUID,
    prompt_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> dict[str, object]:
    return FrozenLobbyModelSelection(
        canonical_slug="openai/byok-test-model",
        catalog_snapshot_id=uuid.uuid4(),
        catalog_content_hash="a" * 64,
        catalog_entry_id=uuid.uuid4(),
        provider_id=provider_id,
        prompt_version_id=prompt_id,
        prompt_hash="b" * 64,
        schema_hash="c" * 64,
        adapter_version="openrouter-adapter-v1",
        inference_params={},
        routing_preferences=OpenRouterProviderPreferences(),
        funding_source="byok",
        funding_owner_ref=f"principal:{owner_id}",
    ).model_dump(mode="json")


async def _seed_byok_game(
    session_factory: async_sessionmaker[AsyncSession], *, secret: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create one launched custom table with a host and an invited human."""
    async with session_factory() as session, session.begin():
        host = Principal(kind="account")
        invitee = Principal(kind="account")
        session.add_all([host, invitee])
        await session.flush()
        host_credential = await _seed_credential(session, principal_id=host.id, secret=secret)
        invitee_credential = await _seed_credential(
            session, principal_id=invitee.id, secret=_INVITEE_KEY
        )
        provider = ModelProvider(
            name=f"openrouter-{uuid.uuid4()}",
            base_url="https://openrouter.example.test/api/v1",
            auth_secret_ref="env:PLATFORM_KEY_MUST_NEVER_BE_USED",
        )
        prompt = PromptVersion(
            ruleset_id="mini7_v1",
            version=f"byok-{uuid.uuid4()}",
            system_prompt="system",
            developer_prompt="developer",
            response_schema={"type": "object"},
            prompt_hash=f"prompt-{uuid.uuid4()}",
        )
        session.add_all([provider, prompt])
        await session.flush()
        config = ModelConfig(
            provider_id=provider.id,
            model_name="byok-test-model",
            litellm_model_id="openai/byok-test-model",
            model_version="v1",
            default_temperature=0.0,
            default_top_p=1.0,
            default_max_output_tokens=256,
            supports_structured_outputs=True,
        )
        session.add(config)
        await session.flush()
        build = AgentBuild(
            display_name="BYOK test build",
            model_config_id=config.id,
            prompt_version_id=prompt.id,
            adapter_version="openrouter-adapter-v1",
            inference_params={},
            active=True,
        )
        league = await session.scalar(
            select(League).where(
                League.ruleset_id == "mini7_v1",
                League.ranked.is_(False),
                League.kind == LeagueKind.HUMANS_INCLUDED.value,
            )
        )
        if league is None:
            league = League(
                name=f"BYOK humans {uuid.uuid4()}",
                ruleset_id="mini7_v1",
                ranked=False,
                kind=LeagueKind.HUMANS_INCLUDED.value,
            )
            session.add(league)
        game = Game(
            ruleset_id="mini7_v1",
            game_seed=f"byok-game-{uuid.uuid4()}",
            status="CREATED",
            identity_mode="TRANSPARENT",
        )
        session.add_all([build, game])
        await session.flush()
        lobby = Lobby(
            ruleset_id="mini7_v1",
            identity_mode="TRANSPARENT",
            invite_token=f"byok-{uuid.uuid4()}",
            theme_pack_id=None,
            stakes="CASUAL",
            lobby_seed=f"byok-lobby-{uuid.uuid4()}",
            host_principal_id=host.id,
            league_id=league.id,
            game_id=game.id,
            status="LAUNCHED",
        )
        session.add(lobby)
        await session.flush()
        for index in range(7):
            human = index < 2
            session.add(
                LobbySeat(
                    lobby_id=lobby.id,
                    seat_index=index,
                    seat_kind="HUMAN" if human else "AI",
                    agent_build_id=None if human else build.id,
                    model_selection=(
                        None
                        if human
                        else _selection(
                            provider_id=provider.id,
                            prompt_id=prompt.id,
                            owner_id=host.id,
                        )
                    ),
                )
            )
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=f"P{index + 1:02d}",
                    seat_index=index,
                    agent_build_id=None if human else build.id,
                    seat_kind=SeatKind.HUMAN.value if human else SeatKind.AI.value,
                    occupant_principal_id=host.id
                    if index == 0
                    else invitee.id
                    if index == 1
                    else None,
                    role=Role.VILLAGER.value,
                    faction=Faction.TOWN.value,
                    alive=True,
                )
            )
        return game.id, host.id, host_credential.id, invitee_credential.id


@pytest.mark.asyncio
async def test_byok_runtime_isolates_host_keys_stamps_calls_and_excludes_platform_spend(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_a, host_a, credential_a, invitee_a = await _seed_byok_game(
        session_factory, secret=_HOST_KEY_A
    )
    game_b, host_b, credential_b, invitee_b = await _seed_byok_game(
        session_factory, secret=_HOST_KEY_B
    )
    decrypted_principals: list[uuid.UUID] = []
    original_decrypt = CredentialEnvelopeService.decrypt_provider_credential

    def capture_decrypt(self: CredentialEnvelopeService, credential: ProviderCredential) -> str:
        decrypted_principals.append(credential.principal_id)
        return original_decrypt(self, credential)

    monkeypatch.setattr(CredentialEnvelopeService, "decrypt_provider_credential", capture_decrypt)
    sent_keys: list[str] = []

    async def fake_completion(**kwargs: object) -> SimpleNamespace:
        key = kwargs.get("api_key")
        assert isinstance(key, str)
        sent_keys.append(key)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"public_message":null,"private_message":null,'
                            '"action":{"type":"NOOP","target":null},'
                            '"memory_update":"","rationale_summary":null}'
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.000001),
            id="byok-response",
        )

    monkeypatch.setattr("padrino.llm.litellm_adapter.litellm.acompletion", fake_completion)
    settings = _settings()
    runtime_a, runtime_b = await asyncio.gather(
        build_human_lane_runtime(session_factory, game_id=game_a, settings=settings),
        build_human_lane_runtime(session_factory, game_id=game_b, settings=settings),
    )
    byok_seats = tuple(f"P{index:02d}" for index in range(3, 8))
    results_a, results_b = await asyncio.gather(
        asyncio.gather(*(runtime_a.adapter.complete(_observation(seat)) for seat in byok_seats)),
        asyncio.gather(*(runtime_b.adapter.complete(_observation(seat)) for seat in byok_seats)),
    )
    assert all(result.status == "ok" for result in (*results_a, *results_b))
    assert set(sent_keys) == {_HOST_KEY_A, _HOST_KEY_B}
    assert sent_keys.count(_HOST_KEY_A) == len(byok_seats)
    assert sent_keys.count(_HOST_KEY_B) == len(byok_seats)
    assert set(decrypted_principals) == {host_a, host_b}
    assert invitee_a not in decrypted_principals
    assert invitee_b not in decrypted_principals

    seat_a = byok_seats[0]
    await _persist_llm_call(
        GamePersistence(
            session_factory=session_factory,
            game_id=game_a,
            llm_call_attributions=runtime_a.llm_call_attributions,
            settings=settings,
        ),
        _observation(seat_a),
        results_a[0],
    )
    async with session_factory() as session:
        row = await session.scalar(select(LlmCall).where(LlmCall.game_id == game_a))
        assert row is not None
        assert row.funding_source == "BYOK_OWNER"
        assert row.openrouter_routing_metadata == {"credential_id": str(credential_a)}
        assert _HOST_KEY_A not in str(row.request_json)
        assert _HOST_KEY_A not in str(row.raw_response)
        assert await global_human_lane_spend_usd(session) == 0.0

    assert set(runtime_a.llm_call_attributions) == set(byok_seats)
    assert set(runtime_b.llm_call_attributions) == set(byok_seats)
    assert all(
        attribution.credential_id == credential_a
        for attribution in runtime_a.llm_call_attributions.values()
    )
    assert all(
        attribution.credential_id == credential_b
        for attribution in runtime_b.llm_call_attributions.values()
    )


@pytest.mark.asyncio
async def test_byok_revocation_fails_closed_before_adapter_assembly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _host_id, credential_id, _invitee_credential_id = await _seed_byok_game(
        session_factory, secret=_HOST_KEY_A
    )
    async with session_factory() as session, session.begin():
        credential = await session.get(ProviderCredential, credential_id)
        assert credential is not None
        credential.revoked_at = credential.created_at
        credential.ciphertext = None
        credential.nonce = None

    async with session_factory() as session:
        with pytest.raises(ByokRuntimeError, match="byok_credential_unavailable"):
            await resolve_byok_runtime_bindings(session, game_id=game_id, settings=_settings())
        with pytest.raises(ByokRuntimeError, match="byok_credential_unavailable"):
            await validate_byok_lobby_credentials(session, game_id=game_id)


@pytest.mark.asyncio
async def test_revoked_byok_game_becomes_provider_failure_no_contest_without_platform_fallback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _host_id, credential_id, _invitee_credential_id = await _seed_byok_game(
        session_factory, secret=_HOST_KEY_A
    )
    async with session_factory() as session, session.begin():
        credential = await session.get(ProviderCredential, credential_id)
        assert credential is not None
        credential.revoked_at = credential.created_at
        credential.ciphertext = None
        credential.nonce = None

    async def unexpected_executor(
        _config: GameConfig,
        _persistence: GamePersistence,
        _adapter: LlmAdapter,
    ) -> None:
        raise AssertionError("a revoked BYOK credential must fail before any platform adapter runs")

    await _run_one_human_game(
        session_factory,
        game_id=game_id,
        semaphore=asyncio.Semaphore(1),
        adapter_factory=None,
        ai_adapter_factory=None,
        game_executor=unexpected_executor,
        settings=_settings(),
        build_production_adapter=True,
        resume=None,
    )

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        calls = list(
            (await session.execute(select(LlmCall).where(LlmCall.game_id == game_id))).scalars()
        )
    assert game is not None
    assert game.status == "FAILED"
    assert game.terminal_result is None
    assert game.last_error == "provider_failure_no_contest"
    assert game.last_error_kind == "byok_credential_unavailable"
    assert calls == []
