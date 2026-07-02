"""US-320: manual ranked rollback matches AI-takeover write-path semantics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.anticheat.rating_rollback import (
    INITIAL_MU as ROLLBACK_INITIAL_MU,
)
from padrino.core.anticheat.rating_rollback import (
    INITIAL_SIGMA as ROLLBACK_INITIAL_SIGMA,
)
from padrino.core.enums import Faction, Role, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import GameSeat, HumanRating
from padrino.db.repositories import games, human_principals, leagues
from padrino.ratings.human_openskill_service import (
    HumanGameResult,
    update_human_ratings_for_game,
)
from padrino.ratings.human_rating_rollback import rollback_ranked_human_rating
from padrino.ratings.openskill_service import (
    INITIAL_MU as WRITE_PATH_INITIAL_MU,
)
from padrino.ratings.openskill_service import (
    INITIAL_SIGMA as WRITE_PATH_INITIAL_SIGMA,
)

_EXCLUDED_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_INCLUDED_NOW = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
_ROLLBACK_NOW = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)

_SEATS: tuple[tuple[str, Role, Faction], ...] = (
    ("P01", Role.MAFIA_GOON, Faction.MAFIA),
    ("P02", Role.MAFIA_GOON, Faction.MAFIA),
    ("P03", Role.DETECTIVE, Faction.TOWN),
    ("P04", Role.DOCTOR, Faction.TOWN),
    ("P05", Role.VILLAGER, Faction.TOWN),
    ("P06", Role.VILLAGER, Faction.TOWN),
    ("P07", Role.VILLAGER, Faction.TOWN),
)


@dataclass(frozen=True, slots=True)
class _Scenario:
    label: str
    target_public_player_id: str
    takeover_public_player_id: str
    winner: Literal["TOWN", "MAFIA", "DRAW"]


async def _principal(session: AsyncSession, label: str) -> uuid.UUID:
    row = await human_principals.create_principal(
        session,
        kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
        display_name=label,
    )
    return row.id


async def _seed_game(
    session: AsyncSession,
    *,
    game_seed: str,
    target_principal_id: uuid.UUID,
    target_public_player_id: str,
    takeover_public_player_id: str | None,
    takeover_principal_id: uuid.UUID | None,
    winner: Literal["TOWN", "MAFIA", "DRAW"],
) -> uuid.UUID:
    game = await games.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=game_seed,
        status="COMPLETED",
    )
    game.terminal_result = {"winner": winner, "reason": "rollback-test"}
    for seat_index, (public_player_id, role, faction) in enumerate(_SEATS):
        occupant_principal_id: uuid.UUID | None = None
        seat_kind = SeatKind.AI.value
        if public_player_id == target_public_player_id:
            occupant_principal_id = target_principal_id
            seat_kind = (
                SeatKind.AI_TAKEOVER.value
                if public_player_id == takeover_public_player_id
                else SeatKind.HUMAN.value
            )
        elif public_player_id == takeover_public_player_id:
            occupant_principal_id = takeover_principal_id
            seat_kind = SeatKind.AI_TAKEOVER.value

        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=public_player_id,
                seat_index=seat_index,
                agent_build_id=None,
                seat_kind=seat_kind,
                occupant_principal_id=occupant_principal_id,
                role=role.value,
                faction=faction.value,
                alive=True,
            )
        )
    return game.id


async def _write_game(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    winner: Literal["TOWN", "MAFIA", "DRAW"],
    now: datetime,
) -> None:
    await update_human_ratings_for_game(
        session,
        league_id=league_id,
        ranked=True,
        rating_enabled=True,
        game_result=HumanGameResult(game_id=game_id, winner=winner),
        now=now,
    )


async def _seed_rollback_history(
    session_factory: async_sessionmaker[AsyncSession],
    scenario: _Scenario,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        target_principal_id = await _principal(session, f"rollback-{scenario.label}-target")
        takeover_principal_id = (
            None
            if scenario.takeover_public_player_id == scenario.target_public_player_id
            else await _principal(session, f"rollback-{scenario.label}-takeover")
        )
        excluded_game_id = await _seed_game(
            session,
            game_seed=f"rollback-{scenario.label}-excluded",
            target_principal_id=target_principal_id,
            target_public_player_id=scenario.target_public_player_id,
            takeover_public_player_id=None,
            takeover_principal_id=None,
            winner="TOWN",
        )
        included_game_id = await _seed_game(
            session,
            game_seed=f"rollback-{scenario.label}-included",
            target_principal_id=target_principal_id,
            target_public_player_id=scenario.target_public_player_id,
            takeover_public_player_id=scenario.takeover_public_player_id,
            takeover_principal_id=takeover_principal_id,
            winner=scenario.winner,
        )
        if takeover_principal_id is not None:
            takeover_warmup_id = await _seed_game(
                session,
                game_seed=f"rollback-{scenario.label}-takeover-warmup",
                target_principal_id=takeover_principal_id,
                target_public_player_id=scenario.takeover_public_player_id,
                takeover_public_player_id=None,
                takeover_principal_id=None,
                winner="TOWN",
            )
            await _write_game(
                session,
                league_id=league.id,
                game_id=takeover_warmup_id,
                winner="TOWN",
                now=_EXCLUDED_NOW,
            )
        await _write_game(
            session,
            league_id=league.id,
            game_id=excluded_game_id,
            winner="TOWN",
            now=_EXCLUDED_NOW,
        )
        await _write_game(
            session,
            league_id=league.id,
            game_id=included_game_id,
            winner=scenario.winner,
            now=_INCLUDED_NOW,
        )
    return target_principal_id, excluded_game_id


async def _seed_fresh_history(
    session_factory: async_sessionmaker[AsyncSession],
    scenario: _Scenario,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        target_principal_id = await _principal(session, f"fresh-{scenario.label}-target")
        takeover_principal_id = (
            None
            if scenario.takeover_public_player_id == scenario.target_public_player_id
            else await _principal(session, f"fresh-{scenario.label}-takeover")
        )
        game_id = await _seed_game(
            session,
            game_seed=f"fresh-{scenario.label}-included",
            target_principal_id=target_principal_id,
            target_public_player_id=scenario.target_public_player_id,
            takeover_public_player_id=scenario.takeover_public_player_id,
            takeover_principal_id=takeover_principal_id,
            winner=scenario.winner,
        )
        if takeover_principal_id is not None:
            takeover_warmup_id = await _seed_game(
                session,
                game_seed=f"fresh-{scenario.label}-takeover-warmup",
                target_principal_id=takeover_principal_id,
                target_public_player_id=scenario.takeover_public_player_id,
                takeover_public_player_id=None,
                takeover_principal_id=None,
                winner="TOWN",
            )
            await _write_game(
                session,
                league_id=league.id,
                game_id=takeover_warmup_id,
                winner="TOWN",
                now=_EXCLUDED_NOW,
            )
        await _write_game(
            session,
            league_id=league.id,
            game_id=game_id,
            winner=scenario.winner,
            now=_INCLUDED_NOW,
        )
    return target_principal_id


async def _rating_for(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
) -> HumanRating:
    async with session_factory() as session:
        return (
            await session.execute(
                select(HumanRating).where(HumanRating.human_player_id == str(principal_id))
            )
        ).scalar_one()


async def _run_rollback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    principal_id: uuid.UUID,
    excluded_game_id: uuid.UUID,
) -> None:
    async with session_factory() as session, session.begin():
        await rollback_ranked_human_rating(
            session,
            principal_id=principal_id,
            excluded_game_ids=[excluded_game_id],
            reason="confirmed_cheating",
            created_by="test-admin",
            now=_ROLLBACK_NOW,
        )


def _assert_rating_matches(rolled_back: HumanRating, fresh: HumanRating) -> None:
    assert rolled_back.games == fresh.games == 1
    assert rolled_back.mu == pytest.approx(fresh.mu)
    assert rolled_back.sigma == pytest.approx(fresh.sigma)
    assert rolled_back.conservative_score == pytest.approx(fresh.conservative_score)
    assert _utc_naive(rolled_back.last_game_at) == _utc_naive(fresh.last_game_at)
    assert _utc_naive(rolled_back.last_game_at) == _utc_naive(_INCLUDED_NOW)


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=None)
    return value.astimezone(UTC).replace(tzinfo=None)


def test_rollback_initial_constants_match_write_path() -> None:
    assert pytest.approx(WRITE_PATH_INITIAL_MU) == ROLLBACK_INITIAL_MU
    assert pytest.approx(WRITE_PATH_INITIAL_SIGMA) == ROLLBACK_INITIAL_SIGMA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    (
        _Scenario(
            label="takeover-teammate",
            target_public_player_id="P03",
            takeover_public_player_id="P04",
            winner="TOWN",
        ),
        _Scenario(
            label="takeover-opponent",
            target_public_player_id="P03",
            takeover_public_player_id="P01",
            winner="TOWN",
        ),
        _Scenario(
            label="takeover-target",
            target_public_player_id="P03",
            takeover_public_player_id="P03",
            winner="TOWN",
        ),
    ),
)
async def test_rollback_recompute_matches_fresh_write_path_with_takeover_roles(
    session_factory: async_sessionmaker[AsyncSession],
    scenario: _Scenario,
) -> None:
    rollback_principal_id, excluded_game_id = await _seed_rollback_history(
        session_factory,
        scenario,
    )
    fresh_principal_id = await _seed_fresh_history(session_factory, scenario)

    await _run_rollback(
        session_factory,
        principal_id=rollback_principal_id,
        excluded_game_id=excluded_game_id,
    )

    rolled_back = await _rating_for(session_factory, rollback_principal_id)
    fresh = await _rating_for(session_factory, fresh_principal_id)
    _assert_rating_matches(rolled_back, fresh)


@pytest.mark.asyncio
async def test_rollback_target_takeover_forfeit_overrides_draw_freeze(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scenario = _Scenario(
        label="takeover-target-draw",
        target_public_player_id="P03",
        takeover_public_player_id="P03",
        winner="DRAW",
    )
    rollback_principal_id, excluded_game_id = await _seed_rollback_history(
        session_factory,
        scenario,
    )
    fresh_principal_id = await _seed_fresh_history(session_factory, scenario)

    await _run_rollback(
        session_factory,
        principal_id=rollback_principal_id,
        excluded_game_id=excluded_game_id,
    )

    rolled_back = await _rating_for(session_factory, rollback_principal_id)
    fresh = await _rating_for(session_factory, fresh_principal_id)
    _assert_rating_matches(rolled_back, fresh)
    assert rolled_back.mu < WRITE_PATH_INITIAL_MU
