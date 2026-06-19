"""US-112: public surfaces exclude unverified ingested games.

The public rating/analytics surfaces (public leaderboard, model rollup,
ladder, recent index) must never include results that originate from
``IngestedGame`` rows whose ``verification_status`` is not ``'verified'`` — a
submitter-scoped key must not be able to pollute the public rankings. House-run
(non-ingested) games are unaffected, and the unverified bundle remains
retrievable on the private/admin detail surface (``GET /public/games/{id}``).

The suite seeds exactly one house game, one verified ingest, and one
unverified ingest, then asserts the unverified result is absent from each
public surface while staying visible on the detail surface.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.db.models import Game
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import ingested_games as ingested_games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories import ratings as ratings_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.ratings.model_rollup import reset_cache as reset_model_rollup_cache
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.public_leaderboard import reset_cache as reset_public_leaderboard_cache
from padrino.settings import get_settings

_RULESET = "mini7_v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _make_bundle(*, game_id: str, town_model: str, mafia_model: str) -> dict[str, Any]:
    """Hand-craft a GameBundle-shaped dict: 2 mafia + 5 town, TOWN wins."""
    seats = [
        {
            "public_player_id": f"P0{i}",
            "seat_index": i - 1,
            "role": "MAFIOSO" if i <= 2 else "VILLAGER",
            "faction": "MAFIA" if i <= 2 else "TOWN",
            "alive": True,
            "death_phase": None,
        }
        for i in range(1, 8)
    ]
    agent_builds = [
        {
            "public_player_id": s["public_player_id"],
            "seat_index": s["seat_index"],
            "display_name": town_model if s["faction"] == "TOWN" else mafia_model,
            "prompt_version": "v1",
            "model_provider": "providerX",
            "model_name": town_model if s["faction"] == "TOWN" else mafia_model,
            "model_version": "1.0",
        }
        for s in seats
    ]
    events = [
        {
            "sequence": 1,
            "event_type": "RolesAssigned",
            "phase": "SETUP",
            "visibility": "PRIVATE",
            "actor_player_id": None,
            "payload": {
                "assignments": [
                    {
                        "public_player_id": s["public_player_id"],
                        "role": s["role"],
                        "faction": s["faction"],
                    }
                    for s in seats
                ]
            },
            "prev_event_hash": "0" * 64,
            "event_hash": "a" * 64,
        },
    ]
    return {
        "schema_version": "padrino.export.v1",
        "ruleset_id": _RULESET,
        "league_id": None,
        "gauntlet_id": None,
        "game_id": game_id,
        "seed": "seed-" + game_id,
        "terminal_result": {"winner": "TOWN", "reason": "TOWN_VOTE", "day_terminated": 2},
        "tip_hash": "c" * 64,
        "agent_builds": agent_builds,
        "game_seats": seats,
        "events": events,
        "signer_fingerprint": None,
        "sig": None,
    }


async def _insert_ingested(
    session: AsyncSession,
    *,
    bundle: dict[str, Any],
    verification_status: str,
) -> None:
    await ingested_games_repo.create(
        session,
        game_id=str(bundle["game_id"]),
        ruleset_id=str(bundle["ruleset_id"]),
        league_id=bundle.get("league_id"),
        gauntlet_id=bundle.get("gauntlet_id"),
        tip_hash=str(bundle["tip_hash"]),
        signer_fingerprint=None,
        verification_status=verification_status,
        submitter_key_id=None,
        bundle=bundle,
    )


async def _make_recent_game(session: AsyncSession, *, game_id: uuid.UUID | None = None) -> Game:
    g = Game(
        id=game_id or uuid.uuid4(),
        ruleset_id=_RULESET,
        game_seed="seed-112",
        status="COMPLETED",
        terminal_result={"winner": "TOWN"},
        broadcast_state=BroadcastState.RECENT.value,
        is_broadcastable=True,
    )
    session.add(g)
    await session.flush()
    return g


async def _make_build(session: AsyncSession, *, display_name: str) -> uuid.UUID:
    from padrino.db.models import PromptVersion

    provider = await providers_repo.create(
        session, name=f"prov-{uuid.uuid4()}", auth_secret_ref="env:MOCK_KEY"
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name=f"mock/{uuid.uuid4()}",
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=512,
        supports_structured_outputs=False,
    )
    pv = PromptVersion(
        ruleset_id=_RULESET,
        version="v1",
        system_prompt="t",
        developer_prompt="t",
        response_schema={},
        prompt_hash=str(uuid.uuid4()),
    )
    session.add(pv)
    await session.flush()
    build = await agent_builds_repo.create(
        session,
        display_name=display_name,
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="1.0",
        inference_params={},
        active=True,
    )
    return build.id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    get_settings.cache_clear()
    reset_public_leaderboard_cache()
    reset_model_rollup_cache()


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


class _Seeded:
    house_game_id: uuid.UUID
    house_build_id: uuid.UUID
    verified_game_id: uuid.UUID
    unverified_game_id: uuid.UUID
    league_id: uuid.UUID


@pytest_asyncio.fixture
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> _Seeded:
    """Seed one house game, one verified ingest, one unverified ingest.

    Each ingest gets a matching local ``Game`` row (its ``game_id`` is the
    string form of the local UUID) so the recent-index filter is exercised on
    a real candidate row rather than passing vacuously.
    """
    out = _Seeded()
    async with session_factory() as session, session.begin():
        # House (non-ingested) game with a ranked Rating so it shows on
        # ladder / model rollup.
        house = await _make_recent_game(session)
        out.house_game_id = house.id
        league = await leagues_repo.create(session, name="L-112", ruleset_id=_RULESET, ranked=True)
        out.league_id = league.id
        build_id = await _make_build(session, display_name="HouseAgent")
        out.house_build_id = build_id
        await ratings_repo.get_or_create_rating(
            session,
            league_id=league.id,
            agent_build_id=build_id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            initial_mu=30.0,
            initial_sigma=5.0,
            initial_conservative_score=30.0 - 15.0,
        )

        # Verified ingest: matching local game + IngestedGame row.
        verified_game = await _make_recent_game(session)
        out.verified_game_id = verified_game.id
        await _insert_ingested(
            session,
            bundle=_make_bundle(
                game_id=str(verified_game.id),
                town_model="verifiedTown",
                mafia_model="verifiedMafia",
            ),
            verification_status="verified",
        )

        # Unverified ingest: matching local game + IngestedGame row.
        unverified_game = await _make_recent_game(session)
        out.unverified_game_id = unverified_game.id
        await _insert_ingested(
            session,
            bundle=_make_bundle(
                game_id=str(unverified_game.id),
                town_model="unverifiedTown",
                mafia_model="unverifiedMafia",
            ),
            verification_status="unverified",
        )
    return out


# ---------------------------------------------------------------------------
# Recent index
# ---------------------------------------------------------------------------


async def test_recent_index_excludes_unverified_ingest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get("/public/recent", headers=_auth(raw))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["game_id"] for item in body["items"]}

    assert str(seeded.house_game_id) in ids
    assert str(seeded.verified_game_id) in ids
    assert str(seeded.unverified_game_id) not in ids
    # total_estimate must not count the excluded game.
    assert body["total_estimate"] == 2


# ---------------------------------------------------------------------------
# Public leaderboard (federated rollup over ingested bundles)
# ---------------------------------------------------------------------------


async def test_public_leaderboard_excludes_unverified_ingest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get("/public/leaderboard", params={"ruleset_id": _RULESET}, headers=_auth(raw))
    assert r.status_code == 200, r.text
    names = {e["display_name"] for e in r.json()["entries"]}

    assert "verifiedTown" in names
    assert "verifiedMafia" in names
    assert "unverifiedTown" not in names
    assert "unverifiedMafia" not in names


# ---------------------------------------------------------------------------
# Ladder (local ratings only — ingests never write Rating rows)
# ---------------------------------------------------------------------------


async def test_ladder_excludes_unverified_ingest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get("/public/ladder", params={"ruleset_id": _RULESET}, headers=_auth(raw))
    assert r.status_code == 200, r.text
    names = {e["display_name"] for e in r.json()["entries"]}

    assert "HouseAgent" in names
    assert "unverifiedTown" not in names
    assert "unverifiedMafia" not in names


# ---------------------------------------------------------------------------
# Model rollup (local ratings only)
# ---------------------------------------------------------------------------


async def test_model_rollup_excludes_unverified_ingest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(seeded.league_id)},
        headers=_auth(raw),
    )
    assert r.status_code == 200, r.text
    names = {e["model_name"] for e in r.json()["entries"]}

    assert "unverifiedTown" not in names
    assert "unverifiedMafia" not in names


# ---------------------------------------------------------------------------
# Private/admin detail surface keeps the unverified bundle visible
# ---------------------------------------------------------------------------


async def test_detail_surface_still_serves_unverified_ingest(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get(f"/public/games/{seeded.unverified_game_id}", headers=_auth(raw))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game_id"] == str(seeded.unverified_game_id)
    assert body["verification_status"] == "unverified"
