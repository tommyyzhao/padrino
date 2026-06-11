"""Tests for the AnalyticsAggregate materialization repo (US-104 producer).

``GET /public/models/{id}/analytics`` reads ``AnalyticsAggregate`` rows; this
module asserts the producer actually writes them: per-game refresh covers every
seated agent, the rollup sums across games, and re-running is idempotent under
the ``(ruleset_id, agent_build_id, version)`` unique constraint.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.analytics.repository import (
    refresh_analytics_aggregate,
    refresh_analytics_aggregates_for_game,
)
from padrino.db.models import AnalyticsAggregate, Game, GameEvent, GameSeat, PromptVersion
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo

pytestmark = pytest.mark.asyncio

_RULESET = "mini7_v1"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

_ROLES_ASSIGNED_PAYLOAD = {
    "assignments": [
        {"public_player_id": "P1", "role": "Mafia", "faction": "MAFIA"},
        {"public_player_id": "P2", "role": "Villager", "faction": "TOWN"},
    ]
}


async def _make_agent_build(session: AsyncSession) -> uuid.UUID:
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
        display_name="RepoTestAgent",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="1.0",
        inference_params={},
        active=True,
    )
    return build.id


def _event(
    game_id: uuid.UUID,
    *,
    sequence: int,
    event_type: str,
    phase: str,
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


async def _make_completed_game(
    session: AsyncSession,
    *,
    agent_build_id: uuid.UUID,
    winner: str = "TOWN",
) -> Game:
    """Seed a COMPLETED two-seat game whose events drive compute_game_analytics."""
    g = Game(
        ruleset_id=_RULESET,
        game_seed=f"repo-{uuid.uuid4()}",
        status="COMPLETED",
        terminal_result={"winner": winner},
    )
    session.add(g)
    await session.flush()
    for idx, (pid, role, faction) in enumerate(
        [("P1", "Mafia", "MAFIA"), ("P2", "Villager", "TOWN")]
    ):
        session.add(
            GameSeat(
                game_id=g.id,
                public_player_id=pid,
                seat_index=idx,
                agent_build_id=agent_build_id,
                role=role,
                faction=faction,
                alive=True,
            )
        )
    events = [
        _event(
            g.id,
            sequence=1,
            event_type="RolesAssigned",
            phase="SETUP",
            visibility="SYSTEM",
            payload=_ROLES_ASSIGNED_PAYLOAD,
        ),
        _event(
            g.id,
            sequence=2,
            event_type="VoteSubmitted",
            phase="DAY_1_VOTE",
            actor_player_id="P2",
            payload={"target": "P1", "is_abstain": False},
        ),
        _event(
            g.id,
            sequence=3,
            event_type="GameTerminated",
            phase="GAME_OVER",
            payload={"winner": winner},
        ),
    ]
    for ev in events:
        session.add(ev)
    await session.flush()
    return g


async def test_refresh_for_game_writes_aggregate_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        game = await _make_completed_game(session, agent_build_id=build_id)
        refreshed = await refresh_analytics_aggregates_for_game(session, game.id, now=_NOW)
        assert len(refreshed) == 1

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AnalyticsAggregate).where(AnalyticsAggregate.agent_build_id == build_id)
            )
        ).scalar_one()
        assert row.ruleset_id == _RULESET
        assert row.version == "v1"
        assert row.games_played == 1
        assert row.voting_total_votes == 1
        assert row.voting_accurate_votes == 1
        rates = {r["role"]: r for r in json.loads(row.role_win_rates_json)}
        assert rates["Villager"]["wins"] == 1
        assert rates["Mafia"]["wins"] == 0


async def test_refresh_rolls_up_across_games_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        g1 = await _make_completed_game(session, agent_build_id=build_id, winner="TOWN")
        g2 = await _make_completed_game(session, agent_build_id=build_id, winner="MAFIA")
        await refresh_analytics_aggregates_for_game(session, g1.id, now=_NOW)
        await refresh_analytics_aggregates_for_game(session, g2.id, now=_NOW)
        # Re-running the refresh must update in place, not violate the
        # (ruleset_id, agent_build_id, version) unique constraint.
        await refresh_analytics_aggregates_for_game(session, g2.id, now=_NOW)

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(AnalyticsAggregate).where(AnalyticsAggregate.agent_build_id == build_id)
                )
            ).scalars()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.games_played == 2
        rates = {r["role"]: r for r in json.loads(row.role_win_rates_json)}
        assert rates["Mafia"] == {"role": "Mafia", "wins": 1, "games": 2}
        assert rates["Villager"] == {"role": "Villager", "wins": 1, "games": 2}


async def test_refresh_skips_unknown_build_and_incomplete_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        build_id = await _make_agent_build(session)
        # Unknown agent build: no row, no crash.
        assert (
            await refresh_analytics_aggregate(
                session, agent_build_id=uuid.uuid4(), ruleset_id=_RULESET, now=_NOW
            )
            is None
        )
        # Build with no completed games: no row.
        assert (
            await refresh_analytics_aggregate(
                session, agent_build_id=build_id, ruleset_id=_RULESET, now=_NOW
            )
            is None
        )
        # Non-completed game: refresh-for-game is a no-op.
        g = Game(ruleset_id=_RULESET, game_seed=f"run-{uuid.uuid4()}", status="RUNNING")
        session.add(g)
        await session.flush()
        assert await refresh_analytics_aggregates_for_game(session, g.id, now=_NOW) == []
