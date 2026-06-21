"""US-151: Cost governance for platform-absorbed human play.

Covers the acceptance criteria:

* per-user/day game + join + inference-$ caps deny past their thresholds;
* a per-lobby cost cap + global circuit breaker throttle NEW lobbies / new turns
  but NEVER kill an active game (the active game's seats/status are untouched);
* ``funding_source`` is recorded on the cost-tracking row (defaults PLATFORM);
* the curated human-eligible pool and the fallback token-price table.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import FundingSource
from padrino.db.models import (
    AgentBuild,
    Game,
    GameSeat,
    League,
    LlmCall,
    Lobby,
    LobbyMember,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
)
from padrino.economics.human_cost_governance import (
    ACTION_CREATE,
    ACTION_JOIN,
    ACTION_LAUNCH,
    HumanAdmitDecision,
    admit_human,
    global_breaker_open,
    global_human_lane_spend_usd,
    human_eligible_pool,
    lobby_breaker_open,
    price_turn_usd,
)
from padrino.settings import Settings

_NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
_TODAY_START = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
_YESTERDAY = _TODAY_START - timedelta(days=1)


def _settings(
    *,
    games_per_day: int = 10,
    joins_per_day: int = 30,
    inference_per_day: float = 5.0,
    lobby_cap: float = 2.0,
    global_breaker: float = 50.0,
) -> Settings:
    return Settings(
        padrino_human_max_games_per_user_per_day=games_per_day,
        padrino_human_max_joins_per_user_per_day=joins_per_day,
        padrino_human_max_inference_usd_per_user_per_day=inference_per_day,
        padrino_human_lobby_cost_cap_usd=lobby_cap,
        padrino_human_global_lobby_cost_breaker_usd=global_breaker,
    )


async def _principal(session: AsyncSession) -> uuid.UUID:
    p = Principal(kind="guest", display_name=None)
    session.add(p)
    await session.flush()
    return p.id


async def _league(session: AsyncSession) -> uuid.UUID:
    league = League(
        name="Humans-Included",
        ruleset_id="mini7_v1",
        ranked=False,
        kind="HUMANS_INCLUDED",
    )
    session.add(league)
    await session.flush()
    return league.id


async def _game(
    session: AsyncSession,
    *,
    status: str = "RUNNING",
    created_at: datetime | None = None,
) -> uuid.UUID:
    g = Game(
        id=uuid.uuid4(),
        ruleset_id="mini7_v1",
        game_seed=f"seed-{uuid.uuid4()}",
        status=status,
    )
    if created_at is not None:
        g.created_at = created_at
    session.add(g)
    await session.flush()
    return g.id


async def _human_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    seat_index: int = 0,
    seat_kind: str = "HUMAN",
) -> None:
    session.add(
        GameSeat(
            game_id=game_id,
            public_player_id=f"P{seat_index:02d}",
            seat_index=seat_index,
            seat_kind=seat_kind,
            occupant_principal_id=principal_id,
            role="VILLAGER",
            faction="TOWN",
            alive=True,
        )
    )
    await session.flush()


async def _call(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    cost: float | None,
    funding_source: str | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    kwargs = {}
    if funding_source is not None:
        kwargs["funding_source"] = funding_source
    call = LlmCall(
        game_id=game_id,
        public_player_id="P01",
        phase="DAY_DISCUSSION",
        request_json={},
        request_prompt_hash="hash",
        status="ok",
        cost_usd=cost,
        **kwargs,
    )
    if created_at is not None:
        call.created_at = created_at
    session.add(call)
    await session.flush()
    return call.id


# ---------------------------------------------------------------------------
# Fallback token-price table
# ---------------------------------------------------------------------------


def test_price_turn_prefers_response_cost() -> None:
    """When LiteLLM returns a response_cost, it is used verbatim."""
    s = _settings()
    cost = price_turn_usd(
        s, response_cost=0.123, model="cerebras/zai-glm-4.7", input_tokens=999, output_tokens=999
    )
    assert cost == 0.123


def test_price_turn_falls_back_when_cost_none() -> None:
    """response_cost=None falls back to the per-1K token-price table."""
    s = Settings(
        padrino_human_fallback_token_price_per_1k={"default": (0.001, 0.002)},
    )
    cost = price_turn_usd(
        s, response_cost=None, model="unknown/model", input_tokens=1000, output_tokens=2000
    )
    # 1000/1000 * 0.001 + 2000/1000 * 0.002 = 0.001 + 0.004
    assert abs(cost - 0.005) < 1e-9


def test_price_turn_fallback_zero_when_no_tokens() -> None:
    """Missing token counts coerce to a zero-cost turn rather than crashing."""
    s = _settings()
    assert (
        price_turn_usd(s, response_cost=None, model="x", input_tokens=None, output_tokens=None)
        == 0.0
    )


# ---------------------------------------------------------------------------
# funding_source recorded on the cost-tracking row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funding_source_defaults_platform(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cost row written without a funding_source defaults to PLATFORM."""
    async with session_factory() as session, session.begin():
        gid = await _game(session)
        cid = await _call(session, game_id=gid, cost=0.5)

    async with session_factory() as session:
        row = await session.get(LlmCall, cid)
        assert row is not None
        assert row.funding_source == FundingSource.PLATFORM.value


@pytest.mark.asyncio
async def test_funding_source_can_record_byok(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The column can record the dormant BYOK_OWNER / SPONSOR_POOL values."""
    async with session_factory() as session, session.begin():
        gid = await _game(session)
        cid = await _call(
            session, game_id=gid, cost=0.5, funding_source=FundingSource.BYOK_OWNER.value
        )

    async with session_factory() as session:
        row = await session.get(LlmCall, cid)
        assert row is not None
        assert row.funding_source == "BYOK_OWNER"


# ---------------------------------------------------------------------------
# Per-user/day admission caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admit_all_clear(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        pid = await _principal(session)

    async with session_factory() as session:
        decision = await admit_human(
            session, _settings(), principal_id=pid, action=ACTION_CREATE, now=_NOW
        )
    assert decision == HumanAdmitDecision(allowed=True, reason="admitted")


@pytest.mark.parametrize(
    ("action", "cap_setting"),
    [
        pytest.param(ACTION_CREATE, "games_per_day", id="create"),
        pytest.param(ACTION_JOIN, "joins_per_day", id="join"),
        pytest.param(ACTION_LAUNCH, "games_per_day", id="launch"),
    ],
)
@pytest.mark.asyncio
async def test_admit_human_is_atomic_under_concurrent_requests(
    tmp_path: Path,
    *,
    action: str,
    cap_setting: str,
) -> None:
    """Concurrent admissions must claim finite per-day slots, not stale counts."""
    from padrino.db.base import Base, create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / f'admit-{action}.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            pid = await _principal(session)

        settings_kwargs = {"games_per_day": 100, "joins_per_day": 100}
        settings_kwargs[cap_setting] = 2
        settings = _settings(**settings_kwargs)
        ready = asyncio.Event()

        async def attempt() -> HumanAdmitDecision:
            await ready.wait()
            async with session_factory() as session, session.begin():
                return await admit_human(
                    session, settings, principal_id=pid, action=action, now=_NOW
                )

        tasks = [asyncio.create_task(attempt()) for _ in range(10)]
        ready.set()
        decisions = await asyncio.gather(*tasks)
    finally:
        await engine.dispose()

    allowed = [decision for decision in decisions if decision.allowed]
    denied = [decision for decision in decisions if not decision.allowed]
    assert len(allowed) == 2
    assert {decision.reason for decision in allowed} == {"admitted"}
    assert len(denied) == 8
    expected_reason = (
        "daily_join_cap_reached" if action == ACTION_JOIN else "daily_game_cap_reached"
    )
    assert {decision.reason for decision in denied} == {expected_reason}


@pytest.mark.asyncio
async def test_daily_game_cap_denies(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """At the games/day cap, create/launch is denied; joins are unaffected."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        for i in range(2):
            gid = await _game(session, created_at=_TODAY_START + timedelta(hours=1))
            await _human_seat(session, game_id=gid, principal_id=pid, seat_index=i)

    s = _settings(games_per_day=2, joins_per_day=30)
    async with session_factory() as session:
        denied = await admit_human(session, s, principal_id=pid, action=ACTION_LAUNCH, now=_NOW)
        join_ok = await admit_human(session, s, principal_id=pid, action=ACTION_JOIN, now=_NOW)
    assert denied == HumanAdmitDecision(allowed=False, reason="daily_game_cap_reached")
    assert join_ok.allowed is True


@pytest.mark.asyncio
async def test_daily_game_cap_ignores_yesterday(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Games created yesterday do not count toward today's games/day cap."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        for i in range(3):
            gid = await _game(session, created_at=_YESTERDAY + timedelta(hours=2))
            await _human_seat(session, game_id=gid, principal_id=pid, seat_index=i)

    async with session_factory() as session:
        decision = await admit_human(
            session, _settings(games_per_day=2), principal_id=pid, action=ACTION_CREATE, now=_NOW
        )
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_daily_join_cap_denies(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """At the joins/day cap, a join is denied."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        league = await _league(session)
        for _ in range(2):
            lobby = Lobby(
                ruleset_id="mini7_v1",
                identity_mode="ANONYMOUS",
                invite_token=str(uuid.uuid4()),
                lobby_seed="s",
                host_principal_id=pid,
                league_id=league,
            )
            session.add(lobby)
            await session.flush()
            session.add(
                LobbyMember(
                    lobby_id=lobby.id,
                    principal_id=pid,
                    is_host=False,
                    joined_at=_TODAY_START + timedelta(hours=1),
                )
            )
            await session.flush()

    async with session_factory() as session:
        decision = await admit_human(
            session, _settings(joins_per_day=2), principal_id=pid, action=ACTION_JOIN, now=_NOW
        )
    assert decision == HumanAdmitDecision(allowed=False, reason="daily_join_cap_reached")


@pytest.mark.asyncio
async def test_daily_inference_cap_denies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At the inference-$/day cap, every action is denied."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session, created_at=_TODAY_START + timedelta(hours=1))
        await _human_seat(session, game_id=gid, principal_id=pid)
        await _call(session, game_id=gid, cost=5.0, created_at=_TODAY_START + timedelta(hours=2))

    async with session_factory() as session:
        decision = await admit_human(
            session,
            _settings(inference_per_day=5.0),
            principal_id=pid,
            action=ACTION_CREATE,
            now=_NOW,
        )
    assert decision == HumanAdmitDecision(allowed=False, reason="daily_inference_cap_reached")


@pytest.mark.asyncio
async def test_daily_inference_cap_uses_charge_time_not_game_created_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A game spanning UTC midnight is charged to the LLM-call day."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session, created_at=_YESTERDAY + timedelta(hours=23))
        await _human_seat(session, game_id=gid, principal_id=pid)
        await _call(session, game_id=gid, cost=5.0, created_at=_TODAY_START + timedelta(hours=1))

    async with session_factory() as session:
        decision = await admit_human(
            session,
            _settings(inference_per_day=5.0),
            principal_id=pid,
            action=ACTION_CREATE,
            now=_NOW,
        )
    assert decision == HumanAdmitDecision(allowed=False, reason="daily_inference_cap_reached")


@pytest.mark.asyncio
async def test_inference_cap_is_per_principal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One principal's spend never counts against another principal."""
    async with session_factory() as session, session.begin():
        spender = await _principal(session)
        other = await _principal(session)
        gid = await _game(session, created_at=_TODAY_START + timedelta(hours=1))
        await _human_seat(session, game_id=gid, principal_id=spender)
        await _call(session, game_id=gid, cost=5.0)

    async with session_factory() as session:
        decision = await admit_human(
            session,
            _settings(inference_per_day=5.0),
            principal_id=other,
            action=ACTION_CREATE,
            now=_NOW,
        )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Circuit breaker: throttle NEW lobbies, never kill an active game
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_breaker_opens_and_admission_denied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cumulative human-lane spend at the global threshold opens the breaker."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session)
        await _human_seat(session, game_id=gid, principal_id=pid)
        await _call(session, game_id=gid, cost=50.0)

    s = _settings(global_breaker=50.0)
    async with session_factory() as session:
        assert await global_breaker_open(session, s) is True
        decision = await admit_human(session, s, principal_id=pid, action=ACTION_CREATE, now=_NOW)
    assert decision == HumanAdmitDecision(allowed=False, reason="breaker_open")


@pytest.mark.asyncio
async def test_lobby_breaker_opens_on_per_lobby_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lobby's game accruing its per-lobby cap opens the lobby breaker."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session)
        await _human_seat(session, game_id=gid, principal_id=pid)
        await _call(session, game_id=gid, cost=2.0)
        lobby = Lobby(
            ruleset_id="mini7_v1",
            identity_mode="ANONYMOUS",
            invite_token=str(uuid.uuid4()),
            lobby_seed="s",
            host_principal_id=pid,
            league_id=await _league(session),
            game_id=gid,
            status="LAUNCHED",
        )
        session.add(lobby)
        await session.flush()
        lobby_id = lobby.id

    s = _settings(lobby_cap=2.0, global_breaker=1000.0)
    async with session_factory() as session:
        loaded = await session.get(Lobby, lobby_id)
        assert loaded is not None
        assert await lobby_breaker_open(session, s, loaded) is True


@pytest.mark.asyncio
async def test_global_human_lane_spend_counts_ai_takeover_seats(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The human-lane global breaker includes seats silently taken over by AI."""
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session)
        await _human_seat(session, game_id=gid, principal_id=pid, seat_kind="AI_TAKEOVER")
        await _call(session, game_id=gid, cost=3.25)

    async with session_factory() as session:
        assert await global_human_lane_spend_usd(session) == 3.25


@pytest.mark.asyncio
async def test_breaker_never_kills_active_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Opening the breaker must NOT mutate an active game's status or seats.

    This is the explicit anti-pattern rejection: the breaker throttles new
    lobbies / new LLM turns, it never boots humans from a running game.
    """
    async with session_factory() as session, session.begin():
        pid = await _principal(session)
        gid = await _game(session, status="RUNNING")
        await _human_seat(session, game_id=gid, principal_id=pid)
        await _call(session, game_id=gid, cost=100.0)
        lobby = Lobby(
            ruleset_id="mini7_v1",
            identity_mode="ANONYMOUS",
            invite_token=str(uuid.uuid4()),
            lobby_seed="s",
            host_principal_id=pid,
            league_id=await _league(session),
            game_id=gid,
            status="LAUNCHED",
        )
        session.add(lobby)
        await session.flush()
        lobby_id = lobby.id

    s = _settings(global_breaker=50.0)
    async with session_factory() as session:
        loaded = await session.get(Lobby, lobby_id)
        assert loaded is not None
        # Breaker is open ...
        assert await lobby_breaker_open(session, s, loaded) is True
        # ... yet the active game and its human seat are entirely untouched.
        game = await session.get(Game, gid)
        assert game is not None
        assert game.status == "RUNNING"
        seats = (
            (await session.execute(select(GameSeat).where(GameSeat.game_id == gid))).scalars().all()
        )
        assert len(seats) == 1
        assert seats[0].alive is True
        assert seats[0].seat_kind == "HUMAN"
        assert seats[0].occupant_principal_id == pid


# ---------------------------------------------------------------------------
# Curated human-eligible pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_eligible_pool_curated_active_builds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The curated pool is the deterministically ordered ACTIVE builds for a ruleset."""
    async with session_factory() as session, session.begin():
        provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
        session.add(provider)
        await session.flush()
        mc = ModelConfig(
            provider_id=provider.id,
            model_name="zai-glm-4.7",
            model_version=None,
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        session.add(mc)
        pv = PromptVersion(
            ruleset_id="mini7_v1",
            version="v1",
            system_prompt="play",
            developer_prompt="json",
            response_schema={"type": "object"},
            prompt_hash="hash-151",
        )
        session.add(pv)
        await session.flush()
        active_ids: list[str] = []
        for i in range(2):
            ab = AgentBuild(
                display_name=f"glm-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            session.add(ab)
            await session.flush()
            active_ids.append(str(ab.id))
        inactive = AgentBuild(
            display_name="glm-off",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={"temperature": 0.7},
            active=False,
        )
        session.add(inactive)
        await session.flush()

    async with session_factory() as session:
        pool = await human_eligible_pool(session, "mini7_v1")
    assert pool == active_ids
