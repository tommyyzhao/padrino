"""Tests for league and gauntlet routes (US-043)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory


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


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _seed_world(
    client: AsyncClient, *, prompt_ruleset: str = mini7_v1.RULESET_ID
) -> tuple[str, list[str]]:
    """Create provider, model config, prompt, 7 agent builds. Return (prompt_id, roster)."""
    pr = await client.post(
        "/model-providers",
        json={"name": "cerebras", "auth_secret_ref": "env:CEREBRAS_API_KEY"},
    )
    assert pr.status_code == 201, pr.text
    provider_id = pr.json()["id"]

    mc = await client.post(
        "/model-configs",
        json={
            "provider_id": provider_id,
            "model_name": "glm-4.7",
            "default_temperature": 0.7,
            "default_top_p": 0.95,
            "default_max_output_tokens": 1024,
            "supports_structured_outputs": True,
        },
    )
    assert mc.status_code == 201, mc.text
    mc_id = mc.json()["id"]

    pv = await client.post(
        "/prompt-versions",
        json={
            "ruleset_id": prompt_ruleset,
            "version": "v1",
            "system_prompt": "sys",
            "developer_prompt": "dev",
            "response_schema": {"type": "object"},
            "prompt_hash": f"ph-{uuid.uuid4().hex}",
        },
    )
    assert pv.status_code == 201, pv.text
    prompt_id = pv.json()["id"]

    roster: list[str] = []
    for i in range(mini7_v1.PLAYER_COUNT):
        ab = await client.post(
            "/agent-builds",
            json={
                "display_name": f"seat-{i}",
                "model_config_id": mc_id,
                "prompt_version_id": prompt_id,
                "adapter_version": "litellm/0.1",
                "inference_params": {},
            },
        )
        assert ab.status_code == 201, ab.text
        roster.append(ab.json()["id"])
    return prompt_id, roster


async def test_create_league_happy_path(client: AsyncClient) -> None:
    response = await client.post(
        "/leagues",
        json={"name": "Spring 2026", "ruleset_id": mini7_v1.RULESET_ID, "ranked": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Spring 2026"
    assert body["ruleset_id"] == mini7_v1.RULESET_ID
    assert body["ranked"] is True
    uuid.UUID(body["id"])
    assert "created_at" in body


async def test_create_league_unranked(client: AsyncClient) -> None:
    response = await client.post(
        "/leagues",
        json={"name": "Casual", "ruleset_id": mini7_v1.RULESET_ID, "ranked": False},
    )
    assert response.status_code == 201
    assert response.json()["ranked"] is False


async def test_create_league_validation_rejects_empty_name(client: AsyncClient) -> None:
    response = await client.post(
        "/leagues",
        json={"name": "", "ruleset_id": mini7_v1.RULESET_ID, "ranked": True},
    )
    assert response.status_code == 422


async def _create_league(client: AsyncClient, *, ranked: bool = True) -> str:
    resp = await client.post(
        "/leagues",
        json={"name": "L", "ruleset_id": mini7_v1.RULESET_ID, "ranked": ranked},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_create_gauntlet_happy_path_with_explicit_seed(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 3,
            "gauntlet_seed": "deadbeef" * 8,
            "roster": roster,
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert response.headers["location"] == f"/gauntlets/{body['gauntlet_id']}"
    uuid.UUID(body["gauntlet_id"])
    assert len(body["game_ids"]) == 3
    for gid in body["game_ids"]:
        uuid.UUID(gid)
    assert len(set(body["game_ids"])) == 3


async def test_create_gauntlet_omitted_seed_is_generated(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 2,
            "roster": roster,
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    # Fetch the gauntlet to confirm a 256-bit hex seed was stored.
    detail = await client.get(f"/gauntlets/{body['gauntlet_id']}")
    assert detail.status_code == 200
    seed = detail.json()["gauntlet_seed"]
    assert isinstance(seed, str)
    assert len(seed) == 64
    int(seed, 16)  # must be valid hex


async def test_create_gauntlet_rejects_roster_too_short(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 1,
            "roster": roster[:6],
        },
    )
    assert response.status_code == 422
    assert "roster" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_roster_too_long(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 1,
            "roster": [*roster, roster[0]],
        },
    )
    assert response.status_code == 422
    assert "roster" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_unknown_agent_build(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)
    bad_roster = [*roster[:6], str(uuid.uuid4())]

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 1,
            "roster": bad_roster,
        },
    )
    assert response.status_code == 422
    assert "agent_build" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_clone_count_zero(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 0,
            "roster": roster,
        },
    )
    assert response.status_code == 422
    assert "clone_count" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_clone_count_too_high(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 101,
            "roster": roster,
        },
    )
    assert response.status_code == 422
    assert "clone_count" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_incompatible_prompt_ruleset(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client, prompt_ruleset="other_v1")
    league_id = await _create_league(client)

    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 1,
            "roster": roster,
        },
    )
    assert response.status_code == 422
    assert "ruleset" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_unknown_league(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    response = await client.post(
        "/gauntlets",
        json={
            "league_id": str(uuid.uuid4()),
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 1,
            "roster": roster,
        },
    )
    assert response.status_code == 422
    assert "league" in response.json()["detail"].lower()


async def test_create_gauntlet_rejects_unknown_prompt(client: AsyncClient) -> None:
    _prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)
    response = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": str(uuid.uuid4()),
            "clone_count": 1,
            "roster": roster,
        },
    )
    assert response.status_code == 422
    assert "prompt" in response.json()["detail"].lower()


async def test_get_gauntlet_returns_status_games_and_diagnostics(client: AsyncClient) -> None:
    prompt_id, roster = await _seed_world(client)
    league_id = await _create_league(client)

    created = await client.post(
        "/gauntlets",
        json={
            "league_id": league_id,
            "ruleset_id": mini7_v1.RULESET_ID,
            "prompt_version_id": prompt_id,
            "clone_count": 3,
            "gauntlet_seed": "abc123" * 10 + "abcd",
            "roster": roster,
        },
    )
    assert created.status_code == 202, created.text
    gauntlet_id = created.json()["gauntlet_id"]
    game_ids = created.json()["game_ids"]

    detail = await client.get(f"/gauntlets/{gauntlet_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == gauntlet_id
    assert body["status"] == "PENDING"
    assert body["league_id"] == league_id
    assert body["ruleset_id"] == mini7_v1.RULESET_ID
    assert body["clone_count"] == 3

    returned_game_ids = [g["id"] for g in body["games"]]
    assert sorted(returned_game_ids) == sorted(game_ids)

    diag = body["diagnostics"]
    # No games have run yet — every aggregate is zero.
    assert diag["games_completed"] == 0
    assert diag["timeout_rate"] == 0.0
    assert diag["invalid_action_rate"] == 0.0
    assert diag["average_public_message_chars"] == 0.0


async def test_get_gauntlet_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/gauntlets/{uuid.uuid4()}")
    assert response.status_code == 404
