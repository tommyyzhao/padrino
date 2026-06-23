"""Tests for continuous matchmaking tick (US-098).

Covers the three assertions from the AC:
  - disabled  -> no games run, returns False
  - enabled + admitted -> one game ran, moderation gate called, game promoted to LIVE
  - cap reached -> skipped (returns False, no game created)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.game_status import GAME_STATUS_FAILED
from padrino.db.models import AgentBuild, Game
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.llm.mock import NoopMockAdapter
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts
from padrino.public.broadcast_index import BroadcastState
from padrino.scheduler.continuous_matchmaking import _load_history, run_continuous_matchmaking_tick
from padrino.settings import Settings

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


class _AlwaysSafeGuard:
    async def check(self, messages: list[str]) -> bool:
        return True


class _TrackingGuard:
    """Records how many times check() is called."""

    def __init__(self) -> None:
        self.call_count = 0

    async def check(self, messages: list[str]) -> bool:
        self.call_count += 1
        return True


async def _seed_roster(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed an active league + 7 active agent builds into the DB."""
    async with session_factory() as session, session.begin():
        canonical: dict[str, Any] = {}
        for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
            canonical[template.role_family.value] = await prompt_versions_repo.create(
                session,
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
        pv = canonical["VANILLA_TOWN"]
        await leagues_repo.create(
            session,
            name="Continuous League",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        provider = await providers_repo.create(
            session, name="mockprov", auth_secret_ref="env:MOCK_KEY"
        )
        for seat in _SEATS:
            mc = await model_configs_repo.create(
                session,
                provider_id=provider.id,
                model_name=f"mock/{seat}",
                litellm_model_id=f"mock/{seat}",
                default_temperature=0.7,
                default_top_p=1.0,
                default_max_output_tokens=512,
                supports_structured_outputs=True,
            )
            await agent_builds_repo.create(
                session,
                display_name=f"Mock {seat}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="mock-v1",
                inference_params={},
                active=True,
            )


async def test_disabled_returns_false_no_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When continuous matchmaking is disabled, tick is a no-op."""
    await _seed_roster(session_factory)
    settings = Settings(padrino_enable_continuous_matchmaking=False)

    result = await run_continuous_matchmaking_tick(
        session_factory,
        settings=settings,
        now=_NOW,
        guard=_AlwaysSafeGuard(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert result is False
    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())
    assert len(games) == 0


async def test_enabled_admitted_game_ran_and_gated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When enabled and admission passes, one game runs and is gated then promoted."""
    await _seed_roster(session_factory)
    guard = _TrackingGuard()
    settings = Settings(padrino_enable_continuous_matchmaking=True)

    result = await run_continuous_matchmaking_tick(
        session_factory,
        settings=settings,
        now=_NOW,
        guard=guard,
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert result is True
    # Moderation gate must have been consulted at least once (one game)
    assert guard.call_count >= 1

    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())

    assert len(games) == 1
    game = games[0]
    assert game.status == "COMPLETED"
    assert game.is_broadcastable is True
    assert game.broadcast_state == BroadcastState.LIVE.value


async def test_cap_reached_skipped_no_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When admission denies (daily cap = 0), tick skips and returns False."""
    await _seed_roster(session_factory)
    settings = Settings(
        padrino_enable_continuous_matchmaking=True,
        padrino_max_games_per_day=0,
    )

    result = await run_continuous_matchmaking_tick(
        session_factory,
        settings=settings,
        now=_NOW,
        guard=_AlwaysSafeGuard(),
        adapter_factory=lambda _a: NoopMockAdapter(),
    )

    assert result is False
    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())
    assert len(games) == 0


async def test_failed_games_are_not_counted_in_match_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_roster(session_factory)
    async with session_factory() as session, session.begin():
        build_ids = list((await session.execute(select(AgentBuild.id))).scalars())
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="failed-history-seed",
            status=GAME_STATUS_FAILED,
        )
        for index, build_id in enumerate(build_ids[: mini7_v1.PLAYER_COUNT]):
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id=f"P{index + 1:02d}",
                seat_index=index,
                agent_build_id=build_id,
                role=Role.VILLAGER.value,
                faction=Faction.TOWN.value,
            )

    async with session_factory() as session:
        history = await _load_history(session)

    assert history == []
