"""US-037: gauntlet scheduler tests.

Validates :func:`padrino.gauntlets.scheduler.create_gauntlet` builds a gauntlet
with deterministically derived per-game seeds, performs roster/clone-count
validation, and commits everything in a single transaction.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1, sk12_v1
from padrino.db.models import Game, Gauntlet, GauntletRosterSlot
from padrino.db.repositories import (
    agent_builds,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.gauntlets.scheduler import create_gauntlet


async def _seed_world(
    session: AsyncSession,
    *,
    roster_size: int = mini7_v1.PLAYER_COUNT,
    ruleset_id: str = mini7_v1.RULESET_ID,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    provider = await providers.create(session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY")
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
        ruleset_id=ruleset_id,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"ph-{uuid.uuid4().hex}",
    )
    league = await leagues.create(session, name="L", ruleset_id=ruleset_id, ranked=True)
    roster: list[uuid.UUID] = []
    for i in range(roster_size):
        ab = await agent_builds.create(
            session,
            display_name=f"seat-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return league.id, pv.id, roster


async def test_create_gauntlet_happy_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        result = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=3,
            gauntlet_seed="seed-happy",
            roster=roster,
        )

    assert isinstance(result.gauntlet_id, uuid.UUID)
    assert len(result.game_ids) == 3
    assert len(set(result.game_ids)) == 3

    async with session_factory() as session:
        g = await session.get(Gauntlet, result.gauntlet_id)
        assert g is not None
        assert g.league_id == league_id
        assert g.prompt_version_id == pv_id
        assert g.clone_count == 3
        assert g.gauntlet_seed == "seed-happy"
        assert g.ruleset_id == mini7_v1.RULESET_ID
        assert g.status == "PENDING"
        assert g.ranked is True

        slots_stmt = (
            select(GauntletRosterSlot)
            .where(GauntletRosterSlot.gauntlet_id == result.gauntlet_id)
            .order_by(GauntletRosterSlot.slot_index)
        )
        slot_rows = list((await session.execute(slots_stmt)).scalars())
        assert [s.slot_index for s in slot_rows] == list(range(mini7_v1.PLAYER_COUNT))
        assert [s.agent_build_id for s in slot_rows] == roster

        games_stmt = (
            select(Game).where(Game.gauntlet_id == result.gauntlet_id).order_by(Game.game_seed)
        )
        game_rows = list((await session.execute(games_stmt)).scalars())
        assert len(game_rows) == 3
        for g_row in game_rows:
            assert g_row.ruleset_id == mini7_v1.RULESET_ID
            assert g_row.status == "CREATED"


async def test_create_gauntlet_seed_derivation_matches_spec(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    gauntlet_seed = "seed-derivation"
    async with session_factory() as session:
        result = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=4,
            gauntlet_seed=gauntlet_seed,
            roster=roster,
        )

    expected = [
        hashlib.sha256(b"game" + gauntlet_seed.encode() + i.to_bytes(4, "big")).hexdigest()
        for i in range(4)
    ]

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Game.game_seed).where(Game.gauntlet_id == result.gauntlet_id)
                )
            ).scalars()
        )
        assert sorted(rows) == sorted(expected)


async def test_create_gauntlet_seed_is_deterministic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        first = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=5,
            gauntlet_seed="determ",
            roster=roster,
        )
    async with session_factory() as session:
        second = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=5,
            gauntlet_seed="determ",
            roster=roster,
        )

    async with session_factory() as session:
        first_seeds = sorted(
            (
                await session.execute(
                    select(Game.game_seed).where(Game.gauntlet_id == first.gauntlet_id)
                )
            )
            .scalars()
            .all()
        )
        second_seeds = sorted(
            (
                await session.execute(
                    select(Game.game_seed).where(Game.gauntlet_id == second.gauntlet_id)
                )
            )
            .scalars()
            .all()
        )
    assert first_seeds == second_seeds


async def test_create_gauntlet_rejects_wrong_roster_size(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, roster_size=6)

    async with session_factory() as session:
        with pytest.raises(ValueError, match="roster"):
            await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=2,
                gauntlet_seed="bad-roster",
                roster=roster,
            )

    async with session_factory() as session, session.begin():
        _, _, big_roster = await _seed_world(session, roster_size=15)

    async with session_factory() as session:
        with pytest.raises(ValueError, match="roster"):
            await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=2,
                gauntlet_seed="bad-roster-15",
                roster=big_roster,
            )


@pytest.mark.parametrize("bad_count", [0, -1, 101, 1000])
async def test_create_gauntlet_rejects_out_of_range_clone_count(
    session_factory: async_sessionmaker[AsyncSession],
    bad_count: int,
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        with pytest.raises(ValueError, match="clone_count"):
            await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=bad_count,
                gauntlet_seed="clone-bounds",
                roster=roster,
            )


async def test_create_gauntlet_rejects_placement_duplicate_build_across_factions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(
            session,
            roster_size=sk12_v1.PLAYER_COUNT,
            ruleset_id=sk12_v1.RULESET_ID,
        )

    bad_roster = [roster[0] for _ in roster]

    async with session_factory() as session:
        with pytest.raises(ValueError, match=r"placement roster.*multiple factions"):
            await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=sk12_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=1,
                gauntlet_seed="sk12-placement-shared-build",
                roster=bad_roster,
            )

    async with session_factory() as session:
        rows = (
            (await session.execute(select(Gauntlet).where(Gauntlet.league_id == league_id)))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.parametrize("good_count", [1, 50, 100])
async def test_create_gauntlet_accepts_boundary_clone_counts(
    session_factory: async_sessionmaker[AsyncSession],
    good_count: int,
) -> None:
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    async with session_factory() as session:
        result = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=good_count,
            gauntlet_seed=f"boundary-{good_count}",
            roster=roster,
        )
    assert len(result.game_ids) == good_count


async def test_create_gauntlet_atomic_on_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If insert fails mid-flight, no partial gauntlet/games/slots should persist."""
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session)

    bad_roster = list(roster)
    bad_roster[3] = uuid.uuid4()  # nonexistent agent_build_id → FK violation on slot

    from sqlalchemy.exc import IntegrityError

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_gauntlet(
                session,
                league_id=league_id,
                ruleset_id=mini7_v1.RULESET_ID,
                prompt_version_id=pv_id,
                clone_count=2,
                gauntlet_seed="atomic",
                roster=bad_roster,
            )

    async with session_factory() as session:
        gauntlets_for_league = list(
            (
                await session.execute(select(Gauntlet).where(Gauntlet.league_id == league_id))
            ).scalars()
        )
        assert gauntlets_for_league == []
        all_games = list((await session.execute(select(Game))).scalars())
        assert all_games == []
