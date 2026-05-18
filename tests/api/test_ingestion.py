"""US-062: ``POST /ingest/game`` verification + persistence tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import (
    SCOPE_ADMIN,
    SCOPE_SPECTATOR,
    SCOPE_SUBMITTER,
    RateLimiter,
    generate_raw_key,
)
from padrino.api.routes.ingest import MAX_BUNDLE_BYTES
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    api_keys as api_keys_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    ingested_games as ingested_games_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.export.bundle import Ed25519Signer, GameBundle, export_game
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-us062-ingest"
_SECRET_AUTH_REF = "env:PADRINO_TEST_INGEST_SECRET"


@pytest.fixture(autouse=True)
def _stub_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_TEST_INGEST_SECRET", "dummy")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def _seed_and_run_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="test-provider", auth_secret_ref=_SECRET_AUTH_REF
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="test-model",
            model_version="v1",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"v{i}",
                system_prompt="s",
                developer_prompt="d",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        await leagues_repo.create(
            session, name="ingest-league", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        game_id = game.id
    builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(mini7_v1.PLAYER_COUNT)}

    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=builds_by_seat,
    )
    await run_game(
        GameConfig(game_id="G-INGEST", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )
    return game_id


async def _build_bundle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
    signer: Ed25519Signer | None = None,
) -> GameBundle:
    game_id = await _seed_and_run_game(session_factory, hash_prefix=hash_prefix)
    async with session_factory() as session:
        return await export_game(session, game_id, signer=signer)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
    label: str,
    submission_public_key: str | None = None,
) -> tuple[str, uuid.UUID]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        obj = await api_keys_repo.create(
            session,
            raw_key=raw,
            scopes=scopes,
            label=label,
            submission_public_key=submission_public_key,
        )
        return raw, obj.id


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def test_signed_happy_path_persists_verified_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = Ed25519Signer.generate()
    raw, key_id = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="submitter-1",
        submission_public_key=signer.public_key_b64(),
    )
    bundle = await _build_bundle(session_factory, hash_prefix="signed-ok", signer=signer)

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["already_ingested"] is False
    assert body["verification_status"] == "verified"
    assert body["game_id"] == bundle.game_id

    async with session_factory() as session:
        row = await ingested_games_repo.get_by_game_id(session, bundle.game_id)
    assert row is not None
    assert row.verification_status == "verified"
    assert row.submitter_key_id == key_id
    assert row.signer_fingerprint == signer.fingerprint
    assert row.tip_hash == bundle.tip_hash


async def test_unsigned_bundle_stored_unverified(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="submitter-noisy",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="unsigned")
    assert bundle.sig is None

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["verification_status"] == "unverified"


async def test_tampered_event_returns_hash_chain_mismatch(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="tamper-bot",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="tamper")

    tampered_events = list(bundle.events)
    target = tampered_events[len(tampered_events) // 2]
    tampered_events[len(tampered_events) // 2] = target.model_copy(
        update={"payload": {**target.payload, "__tamper__": "yes"}}
    )
    tampered = bundle.model_copy(update={"events": tampered_events})

    response = await client.post(
        "/ingest/game",
        content=tampered.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "hash_chain_mismatch"

    # Nothing persisted on a rejected bundle.
    async with session_factory() as session:
        assert await ingested_games_repo.get_by_game_id(session, bundle.game_id) is None


async def test_duplicate_game_id_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="repeater",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="dup")
    payload = bundle.model_dump_json()

    first = await client.post(
        "/ingest/game",
        content=payload,
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/ingest/game",
        content=payload,
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["already_ingested"] is True
    assert body["game_id"] == bundle.game_id


async def test_spectator_scope_is_403(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SPECTATOR],
        label="lurker",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="wrong-scope")

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "insufficient_scope"


async def test_admin_scope_can_submit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_ADMIN],
        label="root",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="admin-ok")

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 201, response.text


async def test_oversized_bundle_returns_413(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="fatso",
    )
    payload = b"x" * (MAX_BUNDLE_BYTES + 1)
    response = await client.post(
        "/ingest/game",
        content=payload,
        headers={**_auth(raw), "content-type": "application/octet-stream"},
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"] == "bundle_too_large"


async def test_signed_bundle_without_registered_key_is_unverifiable(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = Ed25519Signer.generate()
    # Submitter has no `submission_public_key` registered.
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="signed-but-no-key",
    )
    bundle = await _build_bundle(session_factory, hash_prefix="sig-noreg", signer=signer)

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "signature_unverifiable"


async def test_signed_bundle_with_wrong_key_returns_signature_mismatch(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    real_signer = Ed25519Signer.generate()
    decoy = Ed25519Signer.generate()
    # Register the DECOY pubkey on the submitter, but sign with the real signer.
    raw, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="wrong-key",
        submission_public_key=decoy.public_key_b64(),
    )
    bundle = await _build_bundle(session_factory, hash_prefix="wrong-key", signer=real_signer)

    response = await client.post(
        "/ingest/game",
        content=bundle.model_dump_json(),
        headers={**_auth(raw), "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "signature_mismatch"


async def test_admin_keys_route_accepts_submission_public_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_admin, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_ADMIN],
        label="root",
    )
    signer = Ed25519Signer.generate()
    response = await client.post(
        "/admin/keys",
        headers=_auth(raw_admin),
        json={
            "label": "signing-submitter",
            "scopes": [SCOPE_SUBMITTER],
            "submission_public_key": signer.public_key_b64(),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["submission_public_key"] == signer.public_key_b64()
    assert body["scopes"] == [SCOPE_SUBMITTER]


async def test_admin_keys_route_rejects_invalid_submission_public_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_admin, _ = await _seed_key(
        session_factory,
        scopes=[SCOPE_ADMIN],
        label="root",
    )
    response = await client.post(
        "/admin/keys",
        headers=_auth(raw_admin),
        json={
            "label": "bad-pubkey",
            "scopes": [SCOPE_SUBMITTER],
            "submission_public_key": "not-a-valid-key",
        },
    )
    assert response.status_code == 422, response.text
