"""Tests for US-104: Analytics REST endpoints.

Drives:
  GET /public/games/{id}/analytics
  GET /public/models/{id}/analytics

Asserts response shape, spoiler-safety for LIVE games, and 404 handling.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.db.models import AnalyticsAggregate, Game, GameEvent, PromptVersion
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RULESET = "mini7_v1"

_ROLES_ASSIGNED_PAYLOAD = {
    "assignments": [
        {"public_player_id": "P1", "role": "Mafia", "faction": "MAFIA"},
        {"public_player_id": "P2", "role": "Mafia", "faction": "MAFIA"},
        {"public_player_id": "P3", "role": "Detective", "faction": "TOWN"},
        {"public_player_id": "P4", "role": "Doctor", "faction": "TOWN"},
        {"public_player_id": "P5", "role": "Villager", "faction": "TOWN"},
        {"public_player_id": "P6", "role": "Villager", "faction": "TOWN"},
        {"public_player_id": "P7", "role": "Villager", "faction": "TOWN"},
    ]
}


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


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    is_broadcastable: bool = True,
    ruleset_id: str = _RULESET,
) -> Game:
    g = Game(
        ruleset_id=ruleset_id,
        game_seed="test-seed-104",
        status="COMPLETED",
        terminal_result={"winner": "TOWN"},
        broadcast_state=broadcast_state,
        is_broadcastable=is_broadcastable,
    )
    session.add(g)
    await session.flush()
    return g


def _make_event(
    game_id: uuid.UUID,
    *,
    sequence: int,
    event_type: str = "PhaseStarted",
    phase: str = "DAY_1_DISCUSSION_ROUND_1",
    visibility: str = "PUBLIC",
    actor_player_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> GameEvent:
    return GameEvent(
        game_id=game_id,
        sequence=sequence,
        event_type=event_type,
        phase=phase,
        visibility=visibility,
        actor_player_id=actor_player_id,
        payload=payload or {},
        prev_event_hash="0" * 64,
        event_hash=f"{sequence:064x}",
    )


async def _make_game_with_events(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.RECENT.value,
    winner: str = "TOWN",
) -> Game:
    """Create a game with a minimal event log that drives compute_game_analytics."""
    g = await _make_game(session, broadcast_state=broadcast_state, is_broadcastable=True)

    events = [
        _make_event(
            g.id,
            sequence=1,
            event_type="RolesAssigned",
            phase="SETUP",
            visibility="SYSTEM",
            payload=_ROLES_ASSIGNED_PAYLOAD,
        ),
        _make_event(
            g.id,
            sequence=2,
            event_type="VoteSubmitted",
            phase="DAY_1_VOTE",
            visibility="PUBLIC",
            actor_player_id="P3",
            payload={"target": "P1", "is_abstain": False},
        ),
        _make_event(
            g.id,
            sequence=3,
            event_type="PlayerEliminated",
            phase="DAY_1_VOTE",
            visibility="PUBLIC",
            payload={"public_player_id": "P1"},
        ),
        _make_event(
            g.id,
            sequence=4,
            event_type="GameTerminated",
            phase="GAME_OVER",
            visibility="PUBLIC",
            payload={"winner": winner},
        ),
    ]
    for ev in events:
        session.add(ev)
    await session.flush()
    return g


async def _make_agent_build(session: AsyncSession) -> uuid.UUID:
    """Create a minimal ModelProvider → ModelConfig → PromptVersion → AgentBuild."""
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
    pv = PromptVersion(
        ruleset_id=_RULESET,
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
        display_name="TestAgent",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="1.0",
        inference_params={},
        active=True,
    )
    return build.id


async def _make_analytics_aggregate(
    session: AsyncSession,
    *,
    agent_build_id: uuid.UUID,
    ruleset_id: str = _RULESET,
    version: str = "v1",
) -> AnalyticsAggregate:
    role_win_rates = [
        {"role": "Mafia", "wins": 1, "games": 3},
        {"role": "Villager", "wins": 5, "games": 9},
    ]
    survival_curve = [
        {"role": "Mafia", "day": 0, "alive_count": 2, "total_count": 2},
        {"role": "Mafia", "day": 1, "alive_count": 1, "total_count": 2},
    ]
    agg = AnalyticsAggregate(
        ruleset_id=ruleset_id,
        agent_build_id=agent_build_id,
        version=version,
        games_played=10,
        role_win_rates_json=json.dumps(role_win_rates),
        voting_total_votes=30,
        voting_accurate_votes=18,
        survival_curve_json=json.dumps(survival_curve),
        computed_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
    )
    session.add(agg)
    await session.flush()
    return agg


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
# Tests: GET /public/games/{id}/analytics
# ---------------------------------------------------------------------------


async def test_game_analytics_recent_includes_winner(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RECENT game analytics expose winner and role_win_rates."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.RECENT.value)

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["game_id"] == str(game.id)
    assert data["winner"] == "TOWN"
    assert data["role_win_rates"] is not None
    assert isinstance(data["role_win_rates"], list)


async def test_game_analytics_live_hides_winner(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """LIVE game analytics must not expose winner or role_win_rates (spoiler-safe)."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["winner"] is None, "winner must be null for LIVE games"
    assert data["role_win_rates"] is None, "role_win_rates must be null for LIVE games"


async def test_game_analytics_live_includes_safe_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """LIVE game analytics still expose voting accuracy and survival curve."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.LIVE.value)

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    va = data["voting_accuracy"]
    assert "total_votes" in va
    assert "accurate_votes" in va
    assert "rate" in va
    assert isinstance(data["survival_curve"], list)
    assert isinstance(data["claims"], list)
    assert isinstance(data["counter_claims"], list)


async def test_game_analytics_hidden_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HIDDEN (not broadcastable) game returns 404."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session, broadcast_state=BroadcastState.HIDDEN.value, is_broadcastable=False
        )

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 404


async def test_game_analytics_non_broadcastable_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """is_broadcastable=False game returns 404 even if broadcast_state is RECENT."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(
            session, broadcast_state=BroadcastState.RECENT.value, is_broadcastable=False
        )

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 404


async def test_game_analytics_unknown_game_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get(f"/public/games/{uuid.uuid4()}/analytics", headers=_auth(raw))
    assert r.status_code == 404


async def test_game_analytics_voting_accuracy_values(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Voting accuracy counts match the seeded VoteSubmitted events."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        # One vote targeting P1 (MAFIA) → accurate
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.RECENT.value)

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    va = r.json()["voting_accuracy"]
    assert va["total_votes"] == 1
    assert va["accurate_votes"] == 1
    assert va["rate"] == 1.0


async def test_game_analytics_response_shape(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All expected top-level fields are present in the response."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game_with_events(session, broadcast_state=BroadcastState.RECENT.value)

    r = await client.get(f"/public/games/{game.id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    for field in (
        "game_id",
        "ruleset_id",
        "winner",
        "voting_accuracy",
        "survival_curve",
        "role_win_rates",
        "claims",
        "counter_claims",
    ):
        assert field in data, f"missing field: {field}"


# ---------------------------------------------------------------------------
# Tests: GET /public/models/{id}/analytics
# ---------------------------------------------------------------------------


async def test_model_analytics_returns_stored_aggregate(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Returns stored AnalyticsAggregate for a known agent_build_id."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        await _make_analytics_aggregate(session, agent_build_id=build_id)

    r = await client.get(f"/public/models/{build_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    assert data["agent_build_id"] == str(build_id)
    assert data["ruleset_id"] == _RULESET
    assert data["version"] == "v1"
    assert data["games_played"] == 10


async def test_model_analytics_response_shape(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All expected top-level fields are present in the model analytics response."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        await _make_analytics_aggregate(session, agent_build_id=build_id)

    r = await client.get(f"/public/models/{build_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    data = r.json()
    for field in (
        "agent_build_id",
        "ruleset_id",
        "version",
        "games_played",
        "role_win_rates",
        "voting_accuracy",
        "survival_curve",
        "computed_at",
    ):
        assert field in data, f"missing field: {field}"


async def test_model_analytics_voting_accuracy_values(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Voting accuracy rate is correctly computed from stored totals."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        await _make_analytics_aggregate(session, agent_build_id=build_id)

    r = await client.get(f"/public/models/{build_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    va = r.json()["voting_accuracy"]
    assert va["total_votes"] == 30
    assert va["accurate_votes"] == 18
    assert abs(va["rate"] - 18 / 30) < 1e-9


async def test_model_analytics_role_win_rates_shape(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Role win rate entries carry expected fields."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        await _make_analytics_aggregate(session, agent_build_id=build_id)

    r = await client.get(f"/public/models/{build_id}/analytics", headers=_auth(raw))
    assert r.status_code == 200
    rwr = r.json()["role_win_rates"]
    assert len(rwr) == 2
    for entry in rwr:
        for field in ("role", "wins", "games", "rate"):
            assert field in entry


async def test_model_analytics_unknown_agent_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown agent_build_id returns 404."""
    raw = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="viewer")
    r = await client.get(f"/public/models/{uuid.uuid4()}/analytics", headers=_auth(raw))
    assert r.status_code == 404
