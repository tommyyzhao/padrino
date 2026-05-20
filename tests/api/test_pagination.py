"""Cursor pagination + filtering tests for list endpoints (US-055).

The fixtures here are intentionally narrow: they seed N rows via the same
repositories the API uses and then exercise the public surface through the
``CursorPage[T]`` envelope. The keyset cursor is tested for stability, filter
narrowing, invalid-cursor 400s, limit bounds, and resistance to concurrent
inserts that happen between two pages.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.pagination import (
    decode_cursor,
    decode_index_cursor,
    encode_cursor,
    encode_index_cursor,
)
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Game, Gauntlet, ModelProvider
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
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


_BASE_TIME = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)


async def _seed_games(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
    statuses: list[str] | None = None,
    ruleset_id: str = mini7_v1.RULESET_ID,
    gauntlet_id: uuid.UUID | None = None,
    base_time: datetime = _BASE_TIME,
) -> list[uuid.UUID]:
    """Insert ``count`` games with monotonically increasing ``created_at``.

    Returns the inserted ids in insertion order so callers can assert the
    page sequence without re-querying.
    """
    ids: list[uuid.UUID] = []
    async with session_factory() as session:
        for i in range(count):
            status_value = statuses[i] if statuses is not None else "CREATED"
            game = await games_repo.create(
                session,
                ruleset_id=ruleset_id,
                game_seed=f"seed-{i:04d}",
                status=status_value,
                gauntlet_id=gauntlet_id,
            )
            # Force a strictly-monotonic created_at so the keyset cursor is
            # unambiguous even though the wall-clock writes inside one batch
            # would otherwise collide on millisecond boundaries.
            game.created_at = base_time + timedelta(seconds=i)
            ids.append(game.id)
        await session.commit()
    return ids


async def test_cursor_round_trip() -> None:
    now = datetime(2026, 5, 15, 8, 0, 0, tzinfo=UTC)
    row_id = uuid.uuid4()
    token = encode_cursor(now, row_id)
    assert "=" not in token
    decoded_dt, decoded_id = decode_cursor(token)
    assert decoded_dt == now
    assert decoded_id == row_id


async def test_index_cursor_round_trip() -> None:
    token = encode_index_cursor(127)
    assert decode_index_cursor(token) == 127


async def test_games_paginate_through_all_rows_in_stable_order(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_games(session_factory, count=250)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 50}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/games", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body.get("next_cursor")
        if cursor is None:
            break
    # Five pages of 50 — 250 rows total.
    assert pages == 5
    assert len(seen) == 250
    # The cursor preserves insertion order across pages.
    assert seen == [str(gid) for gid in seeded]


async def test_games_filter_narrows_results(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    statuses = ["CREATED"] * 4 + ["COMPLETED"] * 3
    await _seed_games(session_factory, count=7, statuses=statuses)
    response = await client.get("/games", params={"status": "COMPLETED"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 3
    assert {item["status"] for item in body["items"]} == {"COMPLETED"}


async def test_games_filter_by_gauntlet_and_ruleset(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One gauntlet with 2 mini7 games + 1 stray "other_v1" game with no gauntlet.
    async with session_factory() as session:
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v1",
            system_prompt="s",
            developer_prompt="d",
            response_schema={},
            prompt_hash=f"ph-{uuid.uuid4().hex}",
        )
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league.id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv.id,
            clone_count=2,
            gauntlet_seed="ab" * 32,
            ranked=True,
        )
        await session.commit()
        gid = gauntlet.id
    await _seed_games(session_factory, count=2, gauntlet_id=gid)
    await _seed_games(
        session_factory,
        count=1,
        ruleset_id="other_v1",
        base_time=_BASE_TIME + timedelta(hours=1),
    )
    by_gauntlet = await client.get("/games", params={"gauntlet_id": str(gid)})
    assert by_gauntlet.status_code == 200
    assert len(by_gauntlet.json()["items"]) == 2
    by_ruleset = await client.get("/games", params={"ruleset_id": "other_v1"})
    assert by_ruleset.status_code == 200
    assert len(by_ruleset.json()["items"]) == 1


async def test_games_invalid_cursor_returns_400(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_games(session_factory, count=2)
    response = await client.get("/games", params={"cursor": "not-a-valid-cursor"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "invalid_cursor"


async def test_games_limit_above_bound_returns_422(client: AsyncClient) -> None:
    response = await client.get("/games", params={"limit": 201})
    assert response.status_code == 422


async def test_games_limit_below_bound_returns_422(client: AsyncClient) -> None:
    response = await client.get("/games", params={"limit": 0})
    assert response.status_code == 422


async def test_games_unknown_query_param_returns_422(client: AsyncClient) -> None:
    response = await client.get("/games", params={"sortz": "yes"})
    assert response.status_code == 422


async def test_games_concurrent_inserts_dont_duplicate_or_skip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Seed 100 rows in the "past", then paginate the first page, insert 50
    # rows whose created_at is interleaved before AND after the cursor, and
    # confirm the second page (a) doesn't re-emit any first-page id and
    # (b) emits only rows whose created_at strictly exceeds the cursor's.
    initial = await _seed_games(session_factory, count=100)
    first = await client.get("/games", params={"limit": 50})
    assert first.status_code == 200
    first_ids = [item["id"] for item in first.json()["items"]]
    next_cursor = first.json()["next_cursor"]
    assert next_cursor is not None

    # The 50th row (last on page 1) carries this created_at — interleaved
    # inserts before this time should not appear on page 2.
    cutoff_dt, _cutoff_id = decode_cursor(next_cursor)
    interleaved_before = await _seed_games(
        session_factory,
        count=25,
        base_time=cutoff_dt - timedelta(seconds=5_000),
    )
    interleaved_after = await _seed_games(
        session_factory,
        count=25,
        base_time=cutoff_dt + timedelta(seconds=5_000),
    )

    # Drain the rest of the pages.
    seen_after: list[str] = []
    cursor: str | None = next_cursor
    while cursor is not None:
        response = await client.get("/games", params={"limit": 50, "cursor": cursor})
        assert response.status_code == 200
        body = response.json()
        seen_after.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]

    # No duplicates with page 1.
    assert set(seen_after).isdisjoint(set(first_ids))
    # The 50 originals after the cutoff appear, plus the 25 "after"
    # interleaved rows — not the 25 "before" rows (those are older than the
    # cursor and would have appeared on page 1 if they'd existed then).
    expected_after_ids = {str(g) for g in initial[50:]} | {str(g) for g in interleaved_after}
    assert set(seen_after) == expected_after_ids
    # The "before" inserts are invisible to subsequent pages — that's the
    # keyset guarantee: a stable cursor can't replay older rows.
    assert set(seen_after).isdisjoint({str(g) for g in interleaved_before})


async def _seed_provider_set(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with session_factory() as session:
        for i in range(count):
            obj = await providers_repo.create(
                session,
                name=f"provider-{i:03d}",
                auth_secret_ref=f"env:VAR_{i}",
            )
            obj.created_at = _BASE_TIME + timedelta(seconds=i)
            ids.append(obj.id)
        await session.commit()
    return ids


async def test_model_providers_pagination(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_provider_set(session_factory, count=5)
    page1 = await client.get("/model-providers", params={"limit": 2})
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert [it["id"] for it in body1["items"]] == [str(i) for i in ids[:2]]
    page2 = await client.get(
        "/model-providers", params={"limit": 2, "cursor": body1["next_cursor"]}
    )
    body2 = page2.json()
    assert [it["id"] for it in body2["items"]] == [str(i) for i in ids[2:4]]
    page3 = await client.get(
        "/model-providers", params={"limit": 2, "cursor": body2["next_cursor"]}
    )
    body3 = page3.json()
    assert [it["id"] for it in body3["items"]] == [str(ids[4])]
    assert body3["next_cursor"] is None


async def test_model_providers_filter_by_name(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_provider_set(session_factory, count=3)
    response = await client.get("/model-providers", params={"name": "provider-001"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "provider-001"


async def test_model_providers_unknown_param_rejected(client: AsyncClient) -> None:
    response = await client.get("/model-providers", params={"sortz": "asc"})
    assert response.status_code == 422


async def _seed_gauntlets(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
    statuses: list[str] | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with session_factory() as session:
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v1",
            system_prompt="s",
            developer_prompt="d",
            response_schema={},
            prompt_hash=f"ph-{uuid.uuid4().hex}",
        )
        league_id = league.id
        pv_id = pv.id
        ids: list[uuid.UUID] = []
        for i in range(count):
            obj = await gauntlets_repo.create(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=1,
                gauntlet_seed=("g" + f"{i:063d}"),
                ranked=True,
                status=statuses[i] if statuses else "PENDING",
            )
            obj.created_at = _BASE_TIME + timedelta(seconds=i)
            ids.append(obj.id)
        await session.commit()
    return league_id, ids


async def test_gauntlets_pagination_and_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _league_id, ids = await _seed_gauntlets(
        session_factory,
        count=4,
        statuses=["PENDING", "PENDING", "RUNNING", "COMPLETED"],
    )
    all_resp = await client.get("/gauntlets")
    assert all_resp.status_code == 200
    assert len(all_resp.json()["items"]) == 4
    pending = await client.get("/gauntlets", params={"status": "PENDING"})
    assert pending.status_code == 200
    assert len(pending.json()["items"]) == 2
    # Cursor still works under a filter.
    page = await client.get("/gauntlets", params={"limit": 1, "status": "PENDING"})
    assert page.json()["items"][0]["id"] == str(ids[0])
    cursor = page.json()["next_cursor"]
    page2 = await client.get(
        "/gauntlets", params={"limit": 1, "status": "PENDING", "cursor": cursor}
    )
    assert page2.json()["items"][0]["id"] == str(ids[1])
    assert page2.json()["next_cursor"] is None


async def test_gauntlets_unknown_param_rejected(client: AsyncClient) -> None:
    response = await client.get("/gauntlets", params={"foo": "bar"})
    assert response.status_code == 422


async def test_gauntlets_invalid_cursor_returns_400(client: AsyncClient) -> None:
    response = await client.get("/gauntlets", params={"cursor": "###not-base64###"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_cursor"


async def test_leaderboard_accepts_limit_and_cursor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Empty league: the leaderboard still responds with pagination shape.
    async with session_factory() as session:
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        await session.commit()
        league_id = league.id
    response = await client.get(f"/leagues/{league_id}/leaderboard", params={"limit": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "entries" in body
    assert body["next_cursor"] is None


async def test_leaderboard_unknown_param_rejected(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        await session.commit()
        league_id = league.id
    response = await client.get(f"/leagues/{league_id}/leaderboard", params={"foo": "bar"})
    assert response.status_code == 422


async def test_leaderboard_invalid_cursor_returns_400(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        await session.commit()
        league_id = league.id
    response = await client.get(f"/leagues/{league_id}/leaderboard", params={"cursor": "bogus"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_cursor"


async def _seed_full_admin_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:VAR_X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
            default_temperature=0.5,
            default_top_p=1.0,
            default_max_output_tokens=128,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v1",
            system_prompt="s",
            developer_prompt="d",
            response_schema={},
            prompt_hash=f"ph-{uuid.uuid4().hex}",
        )
        await session.commit()
        return provider.id, mc.id, pv.id


async def test_agent_builds_pagination_filter_and_extra_forbid(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _pid, mc_id, pv_id = await _seed_full_admin_set(session_factory)
    async with session_factory() as session:
        for i in range(3):
            obj = await agent_builds_repo.create(
                session,
                display_name=f"ab-{i}",
                model_config_id=mc_id,
                prompt_version_id=pv_id,
                adapter_version="litellm/0.1",
                inference_params={},
                active=(i != 2),
            )
            obj.created_at = _BASE_TIME + timedelta(seconds=i)
        await session.commit()

    full = await client.get("/agent-builds")
    assert full.status_code == 200, full.text
    assert len(full.json()["items"]) == 3

    active = await client.get("/agent-builds", params={"active": "true"})
    assert active.status_code == 200
    assert len(active.json()["items"]) == 2
    assert all(item["active"] for item in active.json()["items"])

    rejected = await client.get("/agent-builds", params={"weird": "1"})
    assert rejected.status_code == 422


async def test_model_configs_and_prompt_versions_list(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pid, mc_id, pv_id = await _seed_full_admin_set(session_factory)
    mcs = await client.get("/model-configs")
    assert mcs.status_code == 200
    ids = [it["id"] for it in mcs.json()["items"]]
    assert str(mc_id) in ids
    by_provider = await client.get("/model-configs", params={"provider_id": str(pid)})
    assert by_provider.status_code == 200
    assert len(by_provider.json()["items"]) == 1

    pvs = await client.get("/prompt-versions")
    assert pvs.status_code == 200
    assert str(pv_id) in [it["id"] for it in pvs.json()["items"]]
    by_ruleset = await client.get("/prompt-versions", params={"ruleset_id": mini7_v1.RULESET_ID})
    assert by_ruleset.status_code == 200
    assert len(by_ruleset.json()["items"]) >= 1


async def test_cursor_is_opaque_string(client: AsyncClient) -> None:
    # Tamper detection — base64 of '{"x":1}' decodes but isn't a 2-tuple.
    bad = encode_index_cursor(0)
    # The index cursor is valid for leaderboard, but invalid for /games.
    response = await client.get("/games", params={"cursor": bad})
    assert response.status_code == 400


# Models referenced for typed assertion that the modules surface symbols.
_REFERENCED_MODELS = (Game, Gauntlet, ModelProvider)
