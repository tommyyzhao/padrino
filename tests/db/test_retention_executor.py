"""Tests for the guarded retention executor (US-116).

Covers:
- Retention disabled -> executor is a no-op, returns None, mutates nothing.
- Dry-run (enabled but dry_run=True) -> mutates nothing, returns a dry-run result.
- Enabled real run -> deletes exactly the planned non-broadcastable games (and
  their child rows) and scrubs heavy llm_call payloads for games past the raw
  TTL, while NEVER touching broadcastable games, ratings, rating events, or
  public replay data.
- Idempotent on re-run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    League,
    LlmCall,
    ModelConfig,
    ModelProvider,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.db.retention_executor import run_retention_executor
from padrino.settings import Settings


def _now() -> datetime:
    return datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _settings(*, enable: bool, dry_run: bool) -> Settings:
    return Settings(
        padrino_enable_retention=enable,
        padrino_retention_dry_run=dry_run,
        padrino_raw_payload_ttl_days=30,
        padrino_non_broadcastable_game_ttl_days=7,
    )


async def _seed_parents(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the FK parent chain; return (agent_build_id, league_id)."""
    provider = ModelProvider(name="prov", auth_secret_ref="ref")
    session.add(provider)
    await session.flush()
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="m",
        default_temperature=0.0,
        default_top_p=1.0,
        default_max_output_tokens=256,
        supports_structured_outputs=True,
    )
    session.add(mc)
    pv = PromptVersion(
        ruleset_id="mini7_v1",
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={},
        prompt_hash="phash",
    )
    session.add(pv)
    await session.flush()
    build = AgentBuild(
        display_name="build",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="a1",
        inference_params={},
        active=True,
    )
    session.add(build)
    league = League(name="L", ruleset_id="mini7_v1", ranked=True)
    session.add(league)
    await session.flush()
    return build.id, league.id


async def _seed_game(
    session: AsyncSession,
    *,
    is_broadcastable: bool,
    completed_days_ago: float,
) -> uuid.UUID:
    game_id = uuid.uuid4()
    completed_at = _now() - timedelta(days=completed_days_ago)
    session.add(
        Game(
            id=game_id,
            ruleset_id="mini7_v1",
            game_seed=f"seed-{game_id}",
            status="COMPLETED",
            completed_at=completed_at,
            is_broadcastable=is_broadcastable,
        )
    )
    await session.flush()
    return game_id


async def _add_llm_call(session: AsyncSession, game_id: uuid.UUID) -> uuid.UUID:
    call_id = uuid.uuid4()
    session.add(
        LlmCall(
            id=call_id,
            game_id=game_id,
            public_player_id="P01",
            phase="DAY_DISCUSSION",
            request_json={"prompt": "heavy payload"},
            request_prompt_hash="hash",
            raw_response="raw heavy response body",
            status="ok",
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
        )
    )
    await session.flush()
    return call_id


async def _add_seat(session: AsyncSession, game_id: uuid.UUID, build_id: uuid.UUID) -> None:
    session.add(
        GameSeat(
            game_id=game_id,
            public_player_id="P01",
            seat_index=0,
            agent_build_id=build_id,
            role="VILLAGER",
            faction="TOWN",
            alive=True,
        )
    )
    await session.flush()


async def _add_event(session: AsyncSession, game_id: uuid.UUID) -> None:
    session.add(
        GameEvent(
            game_id=game_id,
            sequence=0,
            event_type="GameStarted",
            phase="SETUP",
            visibility="PUBLIC",
            payload={},
            prev_event_hash="0" * 64,
            event_hash="a" * 64,
        )
    )
    await session.flush()


async def _add_rating_and_event(
    session: AsyncSession,
    game_id: uuid.UUID,
    build_id: uuid.UUID,
    league_id: uuid.UUID,
) -> None:
    session.add(
        Rating(
            league_id=league_id,
            agent_build_id=build_id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            mu=25.0,
            sigma=8.0,
            conservative_score=1.0,
            games=1,
        )
    )
    session.add(
        RatingEvent(
            league_id=league_id,
            game_id=game_id,
            agent_build_id=build_id,
            scope_type="GLOBAL",
            scope_value="GLOBAL",
            before_mu=25.0,
            before_sigma=8.3,
            after_mu=26.0,
            after_sigma=8.0,
        )
    )
    await session.flush()


async def _count(session: AsyncSession, model: type, **filters: object) -> int:
    stmt = select(func.count()).select_from(model)
    for col, val in filters.items():
        stmt = stmt.where(getattr(model, col) == val)
    return int((await session.execute(stmt)).scalar_one())


class TestDisabled:
    async def test_disabled_is_noop(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session, session.begin():
            old_non_bc = await _seed_game(session, is_broadcastable=False, completed_days_ago=30)
            await _add_llm_call(session, old_non_bc)

        result = await run_retention_executor(
            session_factory, settings=_settings(enable=False, dry_run=False), now=_now()
        )
        assert result is None

        async with session_factory() as session:
            assert await _count(session, Game) == 1
            assert await _count(session, LlmCall) == 1


class TestDryRun:
    async def test_dry_run_mutates_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session, session.begin():
            old_non_bc = await _seed_game(session, is_broadcastable=False, completed_days_ago=30)
            await _add_llm_call(session, old_non_bc)
            old_bc = await _seed_game(session, is_broadcastable=True, completed_days_ago=40)
            await _add_llm_call(session, old_bc)

        result = await run_retention_executor(
            session_factory, settings=_settings(enable=True, dry_run=True), now=_now()
        )
        assert result is not None
        assert result.dry_run is True
        assert result.games_deleted == 0
        assert result.llm_calls_scrubbed == 0

        async with session_factory() as session:
            assert await _count(session, Game) == 2
            assert await _count(session, LlmCall) == 2
            # heavy payloads untouched
            stmt = select(LlmCall.raw_response)
            for raw in (await session.execute(stmt)).scalars():
                assert raw == "raw heavy response body"


class TestEnabledRun:
    async def test_deletes_and_scrubs_exact_candidates(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session, session.begin():
            build_id, league_id = await _seed_parents(session)

            # Non-broadcastable, past delete TTL (7d) -> deleted (cascade children).
            old_non_bc = await _seed_game(session, is_broadcastable=False, completed_days_ago=10)
            await _add_llm_call(session, old_non_bc)
            await _add_seat(session, old_non_bc, build_id)
            await _add_event(session, old_non_bc)

            # Non-broadcastable, within delete TTL -> kept.
            young_non_bc = await _seed_game(session, is_broadcastable=False, completed_days_ago=2)
            await _add_llm_call(session, young_non_bc)

            # Broadcastable, past payload TTL (30d) -> scrub only, NEVER deleted,
            # ratings/rating_events preserved.
            old_bc = await _seed_game(session, is_broadcastable=True, completed_days_ago=40)
            await _add_llm_call(session, old_bc)
            await _add_seat(session, old_bc, build_id)
            await _add_event(session, old_bc)
            await _add_rating_and_event(session, old_bc, build_id, league_id)

            # Broadcastable, within payload TTL -> fully untouched.
            fresh_bc = await _seed_game(session, is_broadcastable=True, completed_days_ago=5)
            await _add_llm_call(session, fresh_bc)

        result = await run_retention_executor(
            session_factory, settings=_settings(enable=True, dry_run=False), now=_now()
        )
        assert result is not None
        assert result.dry_run is False
        assert result.games_deleted == 1
        assert result.llm_calls_scrubbed == 1

        async with session_factory() as session:
            # old_non_bc and all its children are gone.
            assert await _count(session, Game, id=old_non_bc) == 0
            assert await _count(session, LlmCall, game_id=old_non_bc) == 0
            assert await _count(session, GameSeat, game_id=old_non_bc) == 0
            assert await _count(session, GameEvent, game_id=old_non_bc) == 0

            # young_non_bc kept (within TTL).
            assert await _count(session, Game, id=young_non_bc) == 1

            # old_bc preserved entirely (never deleted); ratings + replay intact.
            assert await _count(session, Game, id=old_bc) == 1
            assert await _count(session, GameSeat, game_id=old_bc) == 1
            assert await _count(session, GameEvent, game_id=old_bc) == 1
            assert await _count(session, RatingEvent, game_id=old_bc) == 1
            assert await _count(session, Rating) == 1
            # but its heavy llm payload was scrubbed.
            row = (
                await session.execute(
                    select(LlmCall.raw_response, LlmCall.request_json, LlmCall.cost_usd).where(
                        LlmCall.game_id == old_bc
                    )
                )
            ).one()
            assert row.raw_response is None
            assert row.request_json == {}
            assert row.cost_usd == 0.01  # cost/token metrics preserved

            # fresh_bc fully untouched.
            fresh = (
                await session.execute(
                    select(LlmCall.raw_response).where(LlmCall.game_id == fresh_bc)
                )
            ).scalar_one()
            assert fresh == "raw heavy response body"

    async def test_idempotent_on_rerun(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session, session.begin():
            old_non_bc = await _seed_game(session, is_broadcastable=False, completed_days_ago=10)
            await _add_llm_call(session, old_non_bc)
            old_bc = await _seed_game(session, is_broadcastable=True, completed_days_ago=40)
            await _add_llm_call(session, old_bc)

        first = await run_retention_executor(
            session_factory, settings=_settings(enable=True, dry_run=False), now=_now()
        )
        assert first is not None
        assert first.games_deleted == 1
        assert first.llm_calls_scrubbed == 1

        second = await run_retention_executor(
            session_factory, settings=_settings(enable=True, dry_run=False), now=_now()
        )
        assert second is not None
        # Nothing left to delete; nothing left to scrub (already nulled).
        assert second.games_deleted == 0
        assert second.llm_calls_scrubbed == 0

        async with session_factory() as session:
            assert await _count(session, Game) == 1
            assert await _count(session, Game, id=old_bc) == 1
