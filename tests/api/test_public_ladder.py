"""Tests for US-100: Public ladder endpoints.

Drives ``GET /public/ladder`` and asserts:
* Entries are ordered by ordinal descending.
* Agents with fewer than the threshold games carry provisional=True.
* Per-ruleset isolation: agents from a different ruleset are absent.
* An unknown ruleset returns an empty ladder.
* Pagination (limit + cursor) works correctly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories import ratings as ratings_repo
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RULESET = "mini7_v1"
_RULESET_OTHER = "other_ruleset"


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


async def _make_build(
    session: AsyncSession,
    *,
    display_name: str,
    ruleset_id: str = _RULESET,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create ModelProvider → ModelConfig → PromptVersion → AgentBuild.

    Returns (agent_build_id, prompt_version_id).
    """
    provider = await providers_repo.create(
        session,
        name=f"prov-{uuid.uuid4()}",
        auth_secret_ref="env:MOCK_KEY",
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
    # Create a minimal PromptVersion with a unique hash.
    from padrino.db.models import PromptVersion

    pv = PromptVersion(
        ruleset_id=ruleset_id,
        version="v1",
        system_prompt="test",
        developer_prompt="test",
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
    return build.id, pv.id


async def _make_rating(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    mu: float = INITIAL_MU,
    sigma: float = INITIAL_SIGMA,
    games: int = 0,
) -> None:
    conservative = mu - 3.0 * sigma
    await ratings_repo.get_or_create_rating(
        session,
        league_id=league_id,
        agent_build_id=agent_build_id,
        scope_type=SCOPE_GLOBAL,
        scope_value=SCOPE_VALUE_GLOBAL,
        initial_mu=mu,
        initial_sigma=sigma,
        initial_conservative_score=conservative,
    )
    if games > 0:
        # Update games count.
        from sqlalchemy import select

        from padrino.db.models import Rating

        stmt = select(Rating).where(
            Rating.league_id == league_id,
            Rating.agent_build_id == agent_build_id,
            Rating.scope_type == SCOPE_GLOBAL,
            Rating.scope_value == SCOPE_VALUE_GLOBAL,
        )
        row = (await session.execute(stmt)).scalar_one()
        await ratings_repo.update_rating(
            session,
            row.id,
            mu=mu,
            sigma=sigma,
            conservative_score=conservative,
            games=games,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


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


# ---------------------------------------------------------------------------
# Tests: ordering
# ---------------------------------------------------------------------------


async def test_ladder_ordered_by_ordinal_descending(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agents with higher conservative scores appear first."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L1", ruleset_id=_RULESET, ranked=True)
        # Agent A: high rating (mu=35, sigma=5 → conservative=20 → ordinal=1800)
        build_a_id, _ = await _make_build(session, display_name="AgentA")
        await _make_rating(
            session, league_id=league.id, agent_build_id=build_a_id, mu=35.0, sigma=5.0, games=15
        )
        # Agent B: low rating (mu=15, sigma=5 → conservative=0 → ordinal=1000)
        build_b_id, _ = await _make_build(session, display_name="AgentB")
        await _make_rating(
            session, league_id=league.id, agent_build_id=build_b_id, mu=15.0, sigma=5.0, games=15
        )

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) >= 2
    ordinals = [e["ordinal"] for e in data["entries"]]
    assert ordinals == sorted(ordinals, reverse=True), "entries must be sorted by ordinal DESC"


async def test_ladder_entry_has_expected_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L2", ruleset_id=_RULESET, ranked=True)
        build_id, _ = await _make_build(session, display_name="FieldCheck")
        await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=5)

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}", headers=_auth(raw))
    assert r.status_code == 200
    entry = next(e for e in r.json()["entries"] if e["agent_build_id"] == str(build_id))
    assert "agent_build_id" in entry
    assert "display_name" in entry
    assert "version" in entry
    assert "ordinal" in entry
    assert "provisional" in entry
    assert "games" in entry
    assert "last_game_at" in entry
    assert entry["display_name"] == "FieldCheck"


# ---------------------------------------------------------------------------
# Tests: provisional badge
# ---------------------------------------------------------------------------


async def test_ladder_agent_below_threshold_is_provisional(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agent with fewer than the threshold games is marked provisional."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L3", ruleset_id=_RULESET, ranked=True)
        build_id, _ = await _make_build(session, display_name="NewAgent")
        # Default threshold is 10; 5 games → provisional
        await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=5)

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}", headers=_auth(raw))
    assert r.status_code == 200
    entry = next(e for e in r.json()["entries"] if e["agent_build_id"] == str(build_id))
    assert entry["provisional"] is True
    assert entry["games"] == 5


async def test_ladder_agent_at_threshold_is_established(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agent with exactly the threshold games is not provisional."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L4", ruleset_id=_RULESET, ranked=True)
        build_id, _ = await _make_build(session, display_name="EstAgent")
        # Default threshold is 10; exactly 10 → established
        await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=10)

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}", headers=_auth(raw))
    assert r.status_code == 200
    entry = next(e for e in r.json()["entries"] if e["agent_build_id"] == str(build_id))
    assert entry["provisional"] is False
    assert entry["games"] == 10


# ---------------------------------------------------------------------------
# Tests: per-ruleset isolation
# ---------------------------------------------------------------------------


async def test_ladder_per_ruleset_isolation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agents rated in a different-ruleset league are absent from the target ladder."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league_target = await leagues_repo.create(
            session, name="Target", ruleset_id=_RULESET, ranked=True
        )
        league_other = await leagues_repo.create(
            session, name="Other", ruleset_id=_RULESET_OTHER, ranked=True
        )
        build_target_id, _ = await _make_build(session, display_name="TargetAgent")
        build_other_id, _ = await _make_build(session, display_name="OtherAgent")
        await _make_rating(
            session, league_id=league_target.id, agent_build_id=build_target_id, games=5
        )
        await _make_rating(
            session, league_id=league_other.id, agent_build_id=build_other_id, games=5
        )

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}", headers=_auth(raw))
    assert r.status_code == 200
    build_ids = {e["agent_build_id"] for e in r.json()["entries"]}
    assert str(build_target_id) in build_ids
    assert str(build_other_id) not in build_ids


async def test_ladder_unknown_ruleset_returns_empty(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    r = await client.get("/public/ladder?ruleset_id=nonexistent_ruleset", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["entries"] == []
    assert data["total_estimate"] == 0


# ---------------------------------------------------------------------------
# Tests: pagination
# ---------------------------------------------------------------------------


async def test_ladder_pagination_limit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L5", ruleset_id=_RULESET, ranked=True)
        for i in range(5):
            build_id, _ = await _make_build(session, display_name=f"PagAgent{i}")
            await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=5)

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}&limit=2", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 2
    assert data["total_estimate"] >= 5
    assert data["next_cursor"] is not None


async def test_ladder_pagination_no_overlap(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L6", ruleset_id=_RULESET, ranked=True)
        for i in range(4):
            build_id, _ = await _make_build(session, display_name=f"NoOverlap{i}")
            await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=5)

    r1 = await client.get(f"/public/ladder?ruleset_id={_RULESET}&limit=2", headers=_auth(raw))
    d1 = r1.json()
    assert r1.status_code == 200
    assert len(d1["entries"]) == 2
    assert d1["next_cursor"] is not None

    r2 = await client.get(
        f"/public/ladder?ruleset_id={_RULESET}&limit=2&cursor={d1['next_cursor']}",
        headers=_auth(raw),
    )
    d2 = r2.json()
    assert r2.status_code == 200
    ids1 = {e["agent_build_id"] for e in d1["entries"]}
    ids2 = {e["agent_build_id"] for e in d2["entries"]}
    assert ids1.isdisjoint(ids2), "paginated pages must not overlap"


async def test_ladder_last_page_has_no_cursor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")

    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="L7", ruleset_id=_RULESET, ranked=True)
        for i in range(2):
            build_id, _ = await _make_build(session, display_name=f"LastPage{i}")
            await _make_rating(session, league_id=league.id, agent_build_id=build_id, games=5)

    r = await client.get(f"/public/ladder?ruleset_id={_RULESET}&limit=10", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 2
    assert data["next_cursor"] is None
