"""US-143: terminal-gated endgame reveal endpoint.

``GET /public/games/{id}/reveal`` returns the canonical full per-seat truth
ONLY when the game is terminal (RECENT broadcast state); a LIVE or
not-broadcastable game 404s so nothing is revealed one event early. When
served, the reveal always discloses which seats were human, each seat's
role / faction, the exact model for AI seats, and any takeover provenance —
even in anonymous mode (decision 11).
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
from padrino.core.enums import IdentityMode
from padrino.db.models import Game, GameSeat, PromptVersion
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import get_settings

_RULESET = "mini7_v1"


# ---------------------------------------------------------------------------
# Fixtures + helpers
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


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _make_agent_build(
    session: AsyncSession,
    *,
    provider_name: str,
    model_name: str,
    display_name: str,
) -> uuid.UUID:
    provider = await providers_repo.create(
        session,
        name=provider_name,
        auth_secret_ref="env:MOCK_KEY",
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name=model_name,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=512,
        supports_structured_outputs=False,
    )
    pv = PromptVersion(
        ruleset_id=_RULESET,
        version="v1",
        system_prompt="s",
        developer_prompt="d",
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


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str,
    identity_mode: str = IdentityMode.ANONYMOUS.value,
    winner: str = "TOWN",
) -> Game:
    g = Game(
        ruleset_id=_RULESET,
        game_seed=f"seed-{uuid.uuid4()}",
        status="COMPLETED",
        broadcast_state=broadcast_state,
        is_broadcastable=True,
        identity_mode=identity_mode,
        terminal_result={"winner": winner, "reason": "vote_out"},
    )
    session.add(g)
    await session.flush()
    return g


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_live_game_returns_no_reveal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.LIVE.value)
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                agent_build_id=None,
                seat_kind="HUMAN",
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw))
    assert r.status_code == 404


async def test_hidden_game_returns_no_reveal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.HIDDEN.value)
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw))
    assert r.status_code == 404


async def test_unknown_game_returns_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    r = await client.get(f"/public/games/{uuid.uuid4()}/reveal", headers=_auth(raw))
    assert r.status_code == 404


async def test_terminal_reveal_full_per_seat_truth(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A terminal game reveals human/AI, role, faction, exact model + takeover."""
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        ai_build = await _make_agent_build(
            session,
            provider_name="cerebras",
            model_name="zai-glm-4.7",
            display_name="GLM-Agent",
        )
        takeover_build = await _make_agent_build(
            session,
            provider_name="deepinfra",
            model_name="DeepSeek-V4-Flash",
            display_name="DeepSeek-Agent",
        )
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            winner="MAFIA",
        )
        # Seat 0: human held to the end.
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                agent_build_id=None,
                seat_kind="HUMAN",
                role="DETECTIVE",
                faction="TOWN",
                alive=True,
            )
        )
        # Seat 1: AI that was never human.
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=1,
                agent_build_id=ai_build,
                seat_kind="AI",
                role="MAFIA_GOON",
                faction="MAFIA",
                alive=True,
            )
        )
        # Seat 2: human silently taken over by an AI.
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P02",
                seat_index=2,
                agent_build_id=None,
                seat_kind="AI_TAKEOVER",
                role="VILLAGER",
                faction="TOWN",
                alive=False,
                taken_over_at_phase="DAY_VOTE:2",
                takeover_agent_build_id=takeover_build,
            )
        )
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw))
    assert r.status_code == 200
    body = r.json()
    assert body["game_id"] == str(game_id)
    assert body["ruleset_id"] == _RULESET
    assert body["winner"] == "MAFIA"

    seats = {s["public_player_id"]: s for s in body["seats"]}
    assert [s["seat_index"] for s in body["seats"]] == [0, 1, 2]

    # Human seat: is_human, role/faction, no model.
    p0 = seats["P00"]
    assert p0["is_human"] is True
    assert p0["role"] == "DETECTIVE"
    assert p0["faction"] == "TOWN"
    assert p0["takeover_provenance"] == "HUMAN"
    assert p0["model"] is None

    # AI seat: exact model identity.
    p1 = seats["P01"]
    assert p1["is_human"] is False
    assert p1["takeover_provenance"] == "AI"
    assert p1["model"]["provider"] == "cerebras"
    assert p1["model"]["model_name"] == "zai-glm-4.7"
    assert p1["model"]["display_name"] == "GLM-Agent"

    # Taken-over seat: HUMAN_THEN_AI provenance + the finishing AI's model.
    p2 = seats["P02"]
    assert p2["is_human"] is False
    assert p2["takeover_provenance"] == "HUMAN_THEN_AI"
    assert p2["taken_over_at_phase"] == "DAY_VOTE:2"
    assert p2["model"]["model_name"] == "DeepSeek-V4-Flash"


async def test_reveal_always_served_even_in_anonymous_mode(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision 11: the reveal is served even when the game is ANONYMOUS."""
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        ai_build = await _make_agent_build(
            session,
            provider_name="cerebras",
            model_name="zai-glm-4.7",
            display_name="GLM-Agent",
        )
        game = await _make_game(
            session,
            broadcast_state=BroadcastState.RECENT.value,
            identity_mode=IdentityMode.ANONYMOUS.value,
        )
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                agent_build_id=None,
                seat_kind="HUMAN",
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=1,
                agent_build_id=ai_build,
                seat_kind="AI",
                role="MAFIA_GOON",
                faction="MAFIA",
                alive=True,
            )
        )
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw))
    assert r.status_code == 200
    body = r.json()
    # Even in anonymous mode the reveal exposes the human/AI markers + model.
    seats = {s["public_player_id"]: s for s in body["seats"]}
    assert seats["P00"]["is_human"] is True
    assert seats["P01"]["is_human"] is False
    assert seats["P01"]["model"]["model_name"] == "zai-glm-4.7"
