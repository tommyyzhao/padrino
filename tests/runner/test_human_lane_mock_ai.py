"""US-288 deterministic mock-AI coverage for the human-lane worker."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import LeagueKind, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.game_status import GAME_STATUS_COMPLETED
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    HumanPlayerStats,
    League,
    Lobby,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
    Rating,
    RatingEvent,
)
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import NoopMockAdapter
from padrino.runner.human_durability import replay_state_from_rows
from padrino.runner.human_lane import run_human_lane
from padrino.settings import Settings

_SEED = "us288-human-lane-mock-ai"
_HUMAN_SEAT = "P01"


async def _seed_agent_build(session: AsyncSession) -> uuid.UUID:
    provider = ModelProvider(
        name=f"mock-provider-{uuid.uuid4()}",
        base_url=None,
        auth_secret_ref="CEREBRAS_API_KEY",
    )
    session.add(provider)
    await session.flush()
    model = ModelConfig(
        provider_id=provider.id,
        model_name="mock-model",
        model_version="1",
        default_temperature=0.0,
        default_top_p=1.0,
        default_max_output_tokens=1024,
        supports_structured_outputs=True,
    )
    session.add(model)
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="us288",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"us288-{uuid.uuid4()}",
    )
    session.add(prompt)
    await session.flush()
    build = AgentBuild(
        display_name="US-288 mock build",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="mock",
        inference_params={},
        active=True,
    )
    session.add(build)
    await session.flush()
    return build.id


async def _seed_human_lane_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="guest", display_name=None)
        league = League(
            name=f"Humans-Included US-288 {uuid.uuid4()}",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=False,
            kind=LeagueKind.HUMANS_INCLUDED.value,
        )
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_SEED,
            status="PENDING",
            identity_mode="ANONYMOUS",
        )
        session.add_all([principal, league, game])
        await session.flush()
        session.add(
            Lobby(
                ruleset_id=mini7_v1.RULESET_ID,
                identity_mode="ANONYMOUS",
                invite_token=f"us288-{uuid.uuid4()}",
                theme_pack_id=None,
                stakes="CASUAL",
                lobby_seed=_SEED,
                host_principal_id=principal.id,
                league_id=league.id,
                game_id=game.id,
            )
        )
        ai_build_id = await _seed_agent_build(session)
        for seat in assign_roles(_SEED, mini7_v1):
            is_human = seat.public_player_id == _HUMAN_SEAT
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None if is_human else ai_build_id,
                    seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                    occupant_principal_id=principal.id if is_human else None,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
        await session.flush()
        return game.id


async def _drain_until_status(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
    expected: str,
    *,
    timeout_s: float = 30.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as session:
            game = await session.get(Game, game_id)
            if game is not None and game.status == expected:
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"game {game_id} never reached status {expected!r}")


async def _drain_until_factory_called(
    calls: list[tuple[str, ...]], *, timeout_s: float = 30.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if calls:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("mock AI factory was never called within the budget")


@pytest.mark.asyncio
async def test_mock_ai_human_lane_game_completes_replays_and_skips_scientific_ratings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'human-lane-mock-ai.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    game_id = await _seed_human_lane_game(session_factory)
    mock_factory_calls: list[tuple[str, ...]] = []

    def mock_ai_factory(assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        mock_factory_calls.append(tuple(sorted(assignments)))
        return NoopMockAdapter()

    def fail_real_adapter(*_args: object, **_kwargs: object) -> LlmAdapter:
        raise AssertionError("human-lane mock-AI mode must not build a real provider adapter")

    monkeypatch.setattr(
        "padrino.runner.human_lane.build_heterogeneous_adapter",
        fail_real_adapter,
    )

    try:
        stop = asyncio.Event()
        lane = asyncio.create_task(
            run_human_lane(
                session_factory,
                concurrency=1,
                stop_event=stop,
                ai_adapter_factory=mock_ai_factory,
                poll_interval_s=0.05,
                settings=Settings(
                    padrino_human_lane_mock_ai=True,
                    padrino_human_phase_deadline_seconds=0.001,
                    padrino_human_release_delay_seconds=0.0,
                ),
            )
        )
        try:
            await _drain_until_factory_called(mock_factory_calls)
            await _drain_until_status(session_factory, game_id, GAME_STATUS_COMPLETED)
        finally:
            stop.set()
            await lane

        async with session_factory() as session:
            game = await session.get(Game, game_id)
            rows = list(
                (
                    await session.execute(
                        select(GameEvent)
                        .where(GameEvent.game_id == game_id)
                        .order_by(GameEvent.sequence)
                    )
                ).scalars()
            )
            rating_count = (
                await session.execute(select(func.count()).select_from(Rating))
            ).scalar_one()
            rating_event_count = (
                await session.execute(select(func.count()).select_from(RatingEvent))
            ).scalar_one()
            human_stats = list(
                (
                    await session.execute(
                        select(HumanPlayerStats).where(
                            HumanPlayerStats.ruleset_id == mini7_v1.RULESET_ID
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert game is not None
    assert game.status == GAME_STATUS_COMPLETED
    assert game.completed_at is not None
    assert game.terminal_result is not None
    assert game.terminal_result["winner"] == "DRAW"
    assert mock_factory_calls and set(mock_factory_calls[0]) == {
        seat.public_player_id
        for seat in assign_roles(_SEED, mini7_v1)
        if seat.public_player_id != _HUMAN_SEAT
    }
    replayed_state, replayed_log = replay_state_from_rows(rows)
    assert replayed_state.terminal_result == game.terminal_result["winner"]
    assert tuple(event.event_hash for event in replayed_log.events) == tuple(
        row.event_hash for row in rows
    )
    assert rows[-1].event_hash == game.event_hash_head
    assert rating_count == 0
    assert rating_event_count == 0
    assert len(human_stats) == 1
    assert human_stats[0].games == 1
