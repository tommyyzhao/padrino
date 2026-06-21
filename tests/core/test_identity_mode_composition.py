"""US-126: IdentityMode enum + canonical composition-count function.

Covers:
- the :class:`IdentityMode` enum (ANONYMOUS default, TRANSPARENT);
- the ``games.identity_mode`` column default (a legacy AI-only game defaults to
  ANONYMOUS with no behaviour change);
- the single pure ``composition_summary`` producer of counts-only composition,
  including its start-frozen behaviour across a silent AI takeover.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.composition import composition_summary
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, IdentityMode, Role, SeatKind
from padrino.db.models import Game


def test_identity_mode_enum_values_and_default() -> None:
    assert IdentityMode.ANONYMOUS.value == "ANONYMOUS"
    assert IdentityMode.TRANSPARENT.value == "TRANSPARENT"
    assert {m.value for m in IdentityMode} == {"ANONYMOUS", "TRANSPARENT"}
    # StrEnum: the members compare equal to their string values.
    assert IdentityMode.ANONYMOUS == "ANONYMOUS"


def _seat(kind: SeatKind | None, index: int) -> Seat:
    return Seat(
        public_player_id=f"P{index:02d}",
        seat_index=index,
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        alive=True,
        seat_kind=kind,
    )


def test_composition_summary_counts_humans_and_ai() -> None:
    seats = [
        _seat(SeatKind.HUMAN, 0),
        _seat(SeatKind.HUMAN, 1),
        _seat(SeatKind.AI, 2),
        _seat(SeatKind.AI, 3),
        _seat(SeatKind.AI, 4),
    ]
    assert composition_summary(seats) == {
        "human_count": 2,
        "ai_count": 3,
        "total": 5,
    }


def test_composition_summary_empty() -> None:
    assert composition_summary([]) == {"human_count": 0, "ai_count": 0, "total": 0}


def test_composition_summary_treats_none_kind_as_ai() -> None:
    """A legacy seat with seat_kind=None counts as AI (fail-closed)."""
    seats = [_seat(None, 0), _seat(None, 1), _seat(SeatKind.HUMAN, 2)]
    assert composition_summary(seats) == {
        "human_count": 1,
        "ai_count": 2,
        "total": 3,
    }


def test_composition_summary_frozen_across_takeover() -> None:
    """A silent AI takeover (HUMAN -> AI_TAKEOVER) does NOT change the counts."""
    before = [_seat(SeatKind.HUMAN, 0), _seat(SeatKind.AI, 1)]
    after = [_seat(SeatKind.AI_TAKEOVER, 0), _seat(SeatKind.AI, 1)]
    assert composition_summary(before) == composition_summary(after)
    assert composition_summary(after) == {
        "human_count": 1,
        "ai_count": 1,
        "total": 2,
    }


def test_composition_summary_accepts_mappings_and_raw_kinds() -> None:
    """The canonical fn is data-in: mappings and raw SeatKind/str also work."""
    mixed = [
        {"seat_kind": "HUMAN"},
        {"seat_kind": SeatKind.AI},
        SeatKind.HUMAN,
        "AI",
        {"no_kind": True},  # missing -> AI
    ]
    assert composition_summary(mixed) == {
        "human_count": 2,
        "ai_count": 3,
        "total": 5,
    }


async def test_game_identity_mode_defaults_anonymous(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy AI game persisted without identity_mode loads as ANONYMOUS."""
    async with session_factory() as session:
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="us126-seed",
            status="CREATED",
        )
        session.add(game)
        await session.commit()
        game_id = game.id

    async with session_factory() as session:
        loaded = (await session.execute(_select_game(game_id))).scalar_one()
        assert loaded.identity_mode == "ANONYMOUS"
        assert loaded.identity_mode == IdentityMode.ANONYMOUS


async def test_game_identity_mode_round_trips_transparent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="us126-transparent",
            status="CREATED",
            identity_mode=IdentityMode.TRANSPARENT.value,
        )
        session.add(game)
        await session.commit()
        game_id = game.id

    async with session_factory() as session:
        loaded = (await session.execute(_select_game(game_id))).scalar_one()
        assert loaded.identity_mode == "TRANSPARENT"


def _select_game(game_id: object) -> Select[tuple[Game]]:
    return select(Game).where(Game.id == game_id)
