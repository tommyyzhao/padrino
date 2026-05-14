"""Demo gauntlet bootstrap + runner used by the ``padrino demo-gauntlet`` CLI.

This module is the v1 "fresh-checkout quickstart": it stands up a SQLite
database, seeds the minimal admin rows (provider, model config, prompt
version, league, agent build), schedules one gauntlet, runs every child game
through either the deterministic mock adapter or the real LiteLLM adapter,
backfills ``game_seats`` from each game's final state, and computes the
league leaderboard.

Lives in the impure layer; pure-core does not import it.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.state import Seat
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
from padrino.gauntlets.scheduler import create_gauntlet, derive_game_seed
from padrino.leaderboards.service import compute_leaderboard, entry_to_response
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.adapter import LlmAdapter, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.mock import NoopMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from padrino.settings import Settings

DEMO_ADAPTER_VERSION = "demo-v1"
DEMO_PROMPT_VERSION = "demo-prompt-v1"


def _make_adapter(real: bool, settings: Settings) -> LlmAdapter:
    if not real:
        return NoopMockAdapter()
    routing = RoutingPolicy(
        primary_model=settings.padrino_primary_model,
        fallback_model=settings.padrino_fallback_model,
    )
    build = LlmAgentBuild(
        provider="cerebras",
        model_id=settings.padrino_primary_model,
        prompt_version=DEMO_PROMPT_VERSION,
        inference_params={
            "temperature": settings.padrino_temperature,
            "top_p": settings.padrino_top_p,
        },
        adapter_version=DEMO_ADAPTER_VERSION,
    )
    return LiteLlmAdapter(
        routing_policy=routing,
        agent_build=build,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
    )


async def _seed_minimal_admin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    display_name: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed provider / model_config / prompt_version / league / agent_build.

    Returns ``(league_id, agent_build_id, prompt_version_id)``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session,
            name="demo-provider",
            auth_secret_ref="env:CEREBRAS_API_KEY",
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name=mini7_v1.RULESET_ID,
            default_temperature=mini7_v1.TEMPERATURE,
            default_top_p=mini7_v1.TOP_P,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version=DEMO_PROMPT_VERSION,
            system_prompt="demo",
            developer_prompt="demo",
            response_schema={"type": "object"},
            prompt_hash=f"demo-{uuid.uuid4().hex}",
        )
        league = await leagues_repo.create(
            session,
            name="Demo League",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        agent_build = await agent_builds_repo.create(
            session,
            display_name=display_name,
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version=DEMO_ADAPTER_VERSION,
            inference_params={},
            active=True,
        )
        return league.id, agent_build.id, pv.id


async def _backfill_seats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    seats: tuple[Seat, ...],
    agent_build_id: uuid.UUID,
) -> None:
    """Insert one ``GameSeat`` row per seat from the runner's final state."""
    async with session_factory() as session, session.begin():
        for seat in seats:
            await games_repo.add_seat(
                session,
                game_id=game_id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=agent_build_id,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=seat.alive,
            )


async def run_demo_gauntlet(
    *,
    seed: str,
    clones: int,
    db_url: str,
    real: bool = False,
    settings: Settings | None = None,
    display_name: str = "demo-build",
) -> dict[str, Any]:
    """Bootstrap a demo gauntlet end-to-end and return its leaderboard JSON.

    Creates the schema, seeds the minimal admin rows, schedules ``clones``
    child games, runs each through the chosen adapter (mock by default,
    LiteLLM when ``real=True``), backfills ``game_seats`` from each final
    state, and computes the league leaderboard.

    Returns the leaderboard response body (the same shape the
    ``GET /leagues/{id}/leaderboard`` route emits), augmented with
    ``gauntlet_id`` and ``game_ids`` for caller inspection.
    """
    cfg_settings = settings or Settings()
    engine = create_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)

        league_id, agent_build_id, prompt_version_id = await _seed_minimal_admin(
            session_factory, display_name=display_name
        )
        roster = [agent_build_id] * mini7_v1.PLAYER_COUNT
        async with session_factory() as session:
            created = await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=prompt_version_id,
                clone_count=clones,
                gauntlet_seed=seed,
                roster=roster,
            )

        agent_builds_by_seat = {
            f"P{i + 1:02d}": agent_build_id for i in range(mini7_v1.PLAYER_COUNT)
        }

        gauntlet_tokens = structlog.contextvars.bind_contextvars(
            gauntlet_id=str(created.gauntlet_id),
            league_id=str(league_id),
        )
        try:
            for index, game_id in enumerate(created.game_ids):
                adapter = _make_adapter(real, cfg_settings)
                game_seed = derive_game_seed(seed, index)
                config = GameConfig(
                    game_id=str(game_id),
                    game_seed=game_seed,
                    ruleset_id=mini7_v1.RULESET_ID,
                    timeout_s=float(cfg_settings.padrino_llm_timeout_seconds),
                )
                persistence = GamePersistence(
                    session_factory=session_factory,
                    game_id=game_id,
                    agent_builds=agent_builds_by_seat,
                    league_id=league_id,
                )
                outcome = await run_game(config, adapter, ranked=True, persistence=persistence)
                await _backfill_seats(
                    session_factory,
                    game_id=game_id,
                    seats=outcome.final_state.seats,
                    agent_build_id=agent_build_id,
                )
        finally:
            structlog.contextvars.reset_contextvars(**gauntlet_tokens)

        async with session_factory() as session:
            board = await compute_leaderboard(
                session, league_id=league_id, ruleset_id=mini7_v1.RULESET_ID
            )
    finally:
        await engine.dispose()

    return {
        "leaderboard_id": board.leaderboard_id,
        "ruleset_id": board.ruleset_id,
        "prompt_version": board.prompt_version,
        "rating_model": board.rating_model,
        "entries": [entry_to_response(e) for e in board.entries],
        "gauntlet_id": str(created.gauntlet_id),
        "game_ids": [str(gid) for gid in created.game_ids],
    }


__all__ = [
    "DEMO_ADAPTER_VERSION",
    "DEMO_PROMPT_VERSION",
    "run_demo_gauntlet",
]
