"""US-049: ``GamePersistence`` writes ``game_seats`` from ``RolesAssigned``.

The runner is the single source of truth for seat assignments — earlier code
backfilled them from the final state in :mod:`padrino.demo_gauntlet`, which
left a window where ``game_seats`` was empty mid-game. Now every persisted
run inserts seats inside the same ``session.begin()`` that writes the
``RolesAssigned`` event row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.models import GameEvent, GameSeat
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
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-us049-seats"


async def _seed_setup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"v{i}",
                system_prompt="s",
                developer_prompt="d",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"b-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        await leagues_repo.create(session, name="lg", ruleset_id=mini7_v1.RULESET_ID, ranked=False)
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        game_id = game.id
    builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(mini7_v1.PLAYER_COUNT)}
    return game_id, builds_by_seat


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def test_game_seats_match_roles_assigned_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, builds_by_seat = await _seed_setup(session_factory, hash_prefix="us049-s")
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=builds_by_seat,
    )
    await run_game(
        GameConfig(game_id="G-SEATS", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )

    async with session_factory() as session:
        roles_evt = (
            await session.execute(
                select(GameEvent).where(
                    GameEvent.game_id == game_id,
                    GameEvent.event_type == "RolesAssigned",
                )
            )
        ).scalar_one()
        seats = (
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            )
            .scalars()
            .all()
        )

    assignments = roles_evt.payload["assignments"]
    assert len(seats) == mini7_v1.PLAYER_COUNT
    assert len(assignments) == mini7_v1.PLAYER_COUNT

    by_seat = {s.public_player_id: s for s in seats}
    for entry in assignments:
        seat = by_seat[entry["public_player_id"]]
        assert seat.seat_index == entry["seat_index"]
        assert seat.role == entry["role"]
        assert seat.faction == entry["faction"]
        assert seat.agent_build_id == builds_by_seat[entry["public_player_id"]]
        assert seat.alive is True


async def test_seats_skipped_when_agent_builds_missing_a_seat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, builds_by_seat = await _seed_setup(session_factory, hash_prefix="us049-skip")
    partial = dict(builds_by_seat)
    partial.pop("P07")  # drop one seat from the mapping
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=partial,
    )
    await run_game(
        GameConfig(game_id="G-SEATS-PARTIAL", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )

    async with session_factory() as session:
        seats = (
            (await session.execute(select(GameSeat).where(GameSeat.game_id == game_id)))
            .scalars()
            .all()
        )
    assert seats == []  # all-or-nothing: incomplete mapping means no rows
