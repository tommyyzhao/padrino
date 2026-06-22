"""US-180: mirror-paired gauntlet scheduling and sample diagnostics."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game
from padrino.db.repositories import (
    agent_builds,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.db.repositories import (
    events as events_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.gauntlets.scheduler import create_paired_gauntlet
from padrino.leaderboards.service import compute_leaderboard
from padrino.runner.scheduler import agent_builds_by_seat_for_game


async def _seed_world(
    session: AsyncSession,
    *,
    roster_size: int = mini7_v1.PLAYER_COUNT,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    provider = await providers.create(session, name="cerebras", auth_secret_ref="env:CEREBRAS")
    mc = await model_configs.create(
        session,
        provider_id=provider.id,
        model_name="glm-4.7",
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    pv = await prompt_versions.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"paired-{uuid.uuid4().hex}",
    )
    league = await leagues.create(
        session, name="paired", ruleset_id=mini7_v1.RULESET_ID, ranked=True
    )
    roster: list[uuid.UUID] = []
    for i in range(roster_size):
        ab = await agent_builds.create(
            session,
            display_name=f"build-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return league.id, pv.id, roster


async def _append_chained(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    bodies: list[dict[str, Any]],
) -> None:
    prev = GENESIS_HASH
    for sequence, body in enumerate(bodies):
        sealed = dict(body)
        sealed["sequence"] = sequence
        ev_hash = compute_event_hash(prev, sealed)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=sequence,
            event_type=sealed["event_type"],
            phase=sealed["phase"],
            visibility=sealed["visibility"],
            actor_player_id=sealed.get("actor_player_id"),
            payload=dict(sealed.get("payload", {})),
            prev_event_hash=prev,
            event_hash=ev_hash,
        )
        prev = ev_hash


async def _terminate_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    winner: str,
) -> None:
    await _append_chained(
        session,
        game_id=game_id,
        bodies=[
            {
                "event_type": "GameTerminated",
                "phase": "TERMINAL",
                "visibility": "PUBLIC",
                "actor_player_id": None,
                "payload": {"winner": winner, "reason": "scripted"},
            }
        ],
    )
    await games_repo.update_status(
        session,
        game_id,
        status="COMPLETED",
        terminal_result={"winner": winner, "reason": "scripted", "day_terminated": 1},
    )


async def _seed_pair_seats(
    session: AsyncSession,
    *,
    game: Game,
    roster_slots: list[Any],
) -> None:
    assignments = assign_roles(game.game_seed, mini7_v1)
    mapping = agent_builds_by_seat_for_game(roster_slots, game)
    for seat in assignments:
        await games_repo.add_seat(
            session,
            game_id=game.id,
            public_player_id=seat.public_player_id,
            seat_index=seat.seat_index,
            agent_build_id=mapping[seat.public_player_id],
            role=seat.role.value,
            faction=seat.faction.value,
        )


async def test_create_paired_gauntlet_persists_mirror_legs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        created = await create_paired_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            pair_count=1,
            gauntlet_seed="paired-seed",
            roster=roster,
        )

    async with session_factory() as session:
        games = list(
            (
                await session.execute(
                    select(Game).where(Game.id.in_(created.game_ids)).order_by(Game.pair_leg)
                )
            )
            .scalars()
            .all()
        )
        slots = await gauntlets_repo.list_roster_slots(session, created.gauntlet_id)

    assert len(games) == 2
    assert games[0].pair_id is not None
    assert games[0].pair_id == games[1].pair_id
    assert [game.pair_leg for game in games] == [0, 1]
    assert games[0].game_seed == games[1].game_seed
    assert assign_roles(games[0].game_seed, mini7_v1) == assign_roles(games[1].game_seed, mini7_v1)

    leg0 = agent_builds_by_seat_for_game(slots, games[0])
    leg1 = agent_builds_by_seat_for_game(slots, games[1])
    assert leg0 == {f"P{i + 1:02d}": roster[i] for i in range(mini7_v1.PLAYER_COUNT)}
    assert leg1 == {
        f"P{i + 1:02d}": roster[mini7_v1.PLAYER_COUNT - i - 1] for i in range(mini7_v1.PLAYER_COUNT)
    }
    assert leg0["P01"] == leg1["P07"]
    assert leg0["P07"] == leg1["P01"]


async def test_paired_games_feed_role_samples_and_stay_provisional(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        created = await create_paired_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            pair_count=1,
            gauntlet_seed="paired-samples",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        games = list(
            (
                await session.execute(
                    select(Game).where(Game.id.in_(created.game_ids)).order_by(Game.pair_leg)
                )
            )
            .scalars()
            .all()
        )
        slots = await gauntlets_repo.list_roster_slots(session, created.gauntlet_id)
        for game, winner in zip(games, ("TOWN", "MAFIA"), strict=True):
            await _seed_pair_seats(session, game=game, roster_slots=slots)
            await _terminate_game(session, game_id=game.id, winner=winner)

    async with session_factory() as session:
        leaderboard = await compute_leaderboard(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
        )

    by_build = {entry.agent_build_id: entry for entry in leaderboard.entries}
    first = by_build[roster[0]]
    assert first.provisional is True
    assert first.games == 2
    assert (
        first.faction_breakdown[Faction.TOWN.value]["games"]
        + first.faction_breakdown[Faction.MAFIA.value]["games"]
        == 2
    )
    assert sum(bucket["games"] for bucket in first.role_breakdown.values()) == 2
