"""US-061: signed game-export bundle round-trip + safety guards."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.replay import ReplayHashMismatchError
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
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
from padrino.export.bundle import (
    EXPORT_FORBIDDEN_PAYLOAD_KEYS,
    SCHEMA_VERSION,
    AgentBuildInfo,
    BundlePayloadUnsafeError,
    Ed25519Signer,
    EventEnvelope,
    ExportError,
    GameBundle,
    GameNotExportable,
    GameSeatInfo,
    assert_bundle_payload_safe,
    canonical_bundle_bytes,
    export_game,
    verify_bundle_signature,
)
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-us061-export"
_SECRET_AUTH_REF = "env:PADRINO_TEST_SECRET_REF"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
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
    """Run one mini7_v1 game to TERMINAL and return its persisted game_id."""
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
            session, name="export-league", ruleset_id=mini7_v1.RULESET_ID, ranked=False
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
        GameConfig(game_id="G-EXPORT", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )
    return game_id


# --------------------------------------------------------------------------- #
# Round-trip + canonical determinism
# --------------------------------------------------------------------------- #


async def test_export_game_round_trip_is_byte_identical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_and_run_game(session_factory, hash_prefix="us061-rt")

    async with session_factory() as session:
        bundle_a = await export_game(session, game_id)
    async with session_factory() as session:
        bundle_b = await export_game(session, game_id)

    assert bundle_a.model_dump() == bundle_b.model_dump()
    assert canonical_bundle_bytes(bundle_a) == canonical_bundle_bytes(bundle_b)
    assert bundle_a.schema_version == SCHEMA_VERSION
    assert bundle_a.tip_hash == bundle_b.tip_hash
    assert bundle_a.terminal_result is not None
    assert bundle_a.terminal_result["winner"] == "TOWN"
    assert len(bundle_a.events) > 0
    assert bundle_a.events[0].sequence == 0
    # Final event in the chain is the GameTerminated row.
    assert bundle_a.events[-1].event_type == "GameTerminated"
    # And the seat metadata matches one row per mini7 seat.
    assert len(bundle_a.game_seats) == mini7_v1.PLAYER_COUNT
    assert len(bundle_a.agent_builds) == mini7_v1.PLAYER_COUNT
    # No raw model_provider auth_secret_ref leaks anywhere in the bundle JSON.
    rendered = bundle_a.model_dump_json()
    assert _SECRET_AUTH_REF not in rendered
    assert "auth_secret_ref" not in rendered


# --------------------------------------------------------------------------- #
# Hash chain integrity
# --------------------------------------------------------------------------- #


async def test_hash_chain_integrity_is_verified(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_and_run_game(session_factory, hash_prefix="us061-hc")

    async with session_factory() as session:
        bundle = await export_game(session, game_id)

    # Sanity: the bundle's tip_hash matches the final event's stored hash.
    assert bundle.tip_hash == bundle.events[-1].event_hash

    # Tamper with one mid-chain event payload; replay must mismatch.
    tampered_events = list(bundle.events)
    target = tampered_events[len(tampered_events) // 2]
    bad_payload = dict(target.payload)
    bad_payload["__tamper__"] = "yes"
    tampered_events[len(tampered_events) // 2] = target.model_copy(update={"payload": bad_payload})
    tampered_bundle = bundle.model_copy(update={"events": tampered_events})

    # Recomputing the chain from the tampered events disagrees with the
    # stored event_hash → ReplayHashMismatchError.
    from padrino.export.bundle import _verify_chain

    with pytest.raises(ReplayHashMismatchError):
        _verify_chain(tampered_bundle.events)


# --------------------------------------------------------------------------- #
# Ed25519 signing
# --------------------------------------------------------------------------- #


async def test_signed_bundle_verifies_and_tamper_invalidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_and_run_game(session_factory, hash_prefix="us061-sig")
    signer = Ed25519Signer.generate()

    async with session_factory() as session:
        bundle = await export_game(session, game_id, signer=signer)

    assert bundle.sig is not None
    assert bundle.signer_fingerprint == signer.fingerprint
    pub = signer.public_key_b64()
    assert verify_bundle_signature(bundle, pub) is True

    # Tamper with the events list → signature no longer verifies.
    tampered_events = list(bundle.events)
    head = tampered_events[0]
    tampered_events[0] = head.model_copy(update={"payload": {**head.payload, "__tampered__": True}})
    tampered = bundle.model_copy(update={"events": tampered_events})
    assert verify_bundle_signature(tampered, pub) is False

    # An unsigned bundle never verifies.
    unsigned = bundle.model_copy(update={"sig": None})
    assert verify_bundle_signature(unsigned, pub) is False

    # Verifying with a different key returns False without raising.
    other = Ed25519Signer.generate()
    assert verify_bundle_signature(bundle, other.public_key_b64()) is False


def test_ed25519_signer_from_env_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    seed = b"x" * 32
    seed_b64 = base64.urlsafe_b64encode(seed).decode("ascii")
    monkeypatch.setenv("PADRINO_TEST_EXPORT_KEY", seed_b64)

    signer = Ed25519Signer.from_env("PADRINO_TEST_EXPORT_KEY")
    assert signer.fingerprint == Ed25519Signer.from_seed_b64(seed_b64).fingerprint
    sig = signer.sign(b"hello")
    assert isinstance(sig, str) and len(sig) > 0


def test_ed25519_signer_from_env_rejects_missing_var() -> None:
    with pytest.raises(ExportError):
        Ed25519Signer.from_env("PADRINO_DEFINITELY_NOT_SET_KEY_FOR_TEST")


def test_ed25519_signer_from_seed_rejects_short_seed() -> None:
    import base64

    short = base64.urlsafe_b64encode(b"abc").decode("ascii")
    with pytest.raises(ExportError):
        Ed25519Signer.from_seed_b64(short)


# --------------------------------------------------------------------------- #
# Safety guards over event payloads
# --------------------------------------------------------------------------- #


def test_assert_bundle_payload_safe_passes_on_clean_payloads() -> None:
    safe = [
        EventEnvelope(
            sequence=0,
            event_type="GameCreated",
            phase="PHASE",
            visibility="SYSTEM",
            actor_player_id=None,
            payload={"ruleset_id": "mini7_v1"},
            prev_event_hash="00" * 32,
            event_hash="aa" * 32,
        )
    ]
    assert_bundle_payload_safe(safe)


@pytest.mark.parametrize("bad_key", sorted(EXPORT_FORBIDDEN_PAYLOAD_KEYS))
def test_assert_bundle_payload_safe_flags_forbidden_keys(bad_key: str) -> None:
    events = [
        EventEnvelope(
            sequence=0,
            event_type="GameCreated",
            phase="PHASE",
            visibility="SYSTEM",
            actor_player_id=None,
            payload={"nested": {bad_key: "leaked"}},
            prev_event_hash="00" * 32,
            event_hash="aa" * 32,
        )
    ]
    with pytest.raises(BundlePayloadUnsafeError, match=bad_key):
        assert_bundle_payload_safe(events)


async def test_export_game_rejects_payload_with_forbidden_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id = await _seed_and_run_game(session_factory, hash_prefix="us061-safe")

    # Inject a forbidden key into one persisted event payload to simulate
    # an engine bug or an externally-tampered DB.
    from sqlalchemy import select

    from padrino.db.models import GameEvent

    async with session_factory() as session, session.begin():
        row = (
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        bad = dict(row.payload)
        bad["model_id"] = "gpt-leaked"
        row.payload = bad

    async with session_factory() as session:
        with pytest.raises(BundlePayloadUnsafeError):
            await export_game(session, game_id)


# --------------------------------------------------------------------------- #
# Error surface
# --------------------------------------------------------------------------- #


async def test_export_game_rejects_unknown_game_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(GameNotExportable):
            await export_game(session, uuid.uuid4())


async def test_export_game_rejects_non_terminal_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="seed-unfinished",
            status="RUNNING",
        )
        gid = game.id
    async with session_factory() as session:
        with pytest.raises(GameNotExportable, match="COMPLETED"):
            await export_game(session, gid)


# --------------------------------------------------------------------------- #
# Pydantic-level shape checks (frozen + no extras)
# --------------------------------------------------------------------------- #


def test_agent_build_info_is_frozen_and_forbids_extras() -> None:
    from pydantic import ValidationError

    info = AgentBuildInfo(
        public_player_id="P01",
        seat_index=0,
        display_name="demo",
        prompt_version="v1",
        model_provider="prov",
        model_name="mdl",
        model_version=None,
    )
    with pytest.raises(ValidationError):
        AgentBuildInfo(  # type: ignore[call-arg]
            public_player_id="P01",
            seat_index=0,
            display_name="demo",
            prompt_version="v1",
            model_provider="prov",
            model_name="mdl",
            model_version=None,
            extra="boom",
        )
    # Sanity: frozen — can't mutate.
    with pytest.raises(ValidationError):
        info.display_name = "other"  # type: ignore[misc]


def test_game_seat_info_minimal_round_trip() -> None:
    seat = GameSeatInfo(
        public_player_id="P01",
        seat_index=0,
        role="VILLAGER",
        faction="TOWN",
        alive=True,
        death_phase=None,
    )
    payload = seat.model_dump()
    assert payload["role"] == "VILLAGER"


def test_canonical_bundle_bytes_is_sorted_json() -> None:
    bundle = GameBundle(
        ruleset_id="mini7_v1",
        league_id=None,
        gauntlet_id=None,
        game_id="gid",
        seed="seed",
        terminal_result=None,
        tip_hash="ff" * 32,
        agent_builds=[],
        game_seats=[],
        events=[],
    )
    raw = canonical_bundle_bytes(bundle)
    # Round-trips through json with sort_keys=True identically.
    decoded = json.loads(raw)
    assert canonical_dumps(decoded) == raw
    # The "sig" field is excluded from the signed bytes; only the
    # "signer_fingerprint" field (which carries "sig" as a substring) survives.
    assert "sig" not in decoded
    assert "signer_fingerprint" in decoded
