"""Regression tests for shared human seat authorization helpers."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api import human_actions, human_chat, human_observation, human_turing
from padrino.api.human_seat_auth import resolve_human_game_seat
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat, Principal


def test_human_routes_share_one_seat_authorization_helper() -> None:
    """Action/chat/observation/turing use the same wrong-seat resolver."""
    consumers = (human_actions, human_chat, human_observation, human_turing)

    for module in consumers:
        assert module.resolve_human_game_seat is resolve_human_game_seat
        assert "_resolve_seat" not in module.__dict__


@pytest.mark.asyncio
async def test_seat_auth_fails_closed_when_principal_matches_multiple_game_seats(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A corrupted multi-seat binding must reject instead of picking a seat."""
    async with session_factory() as session, session.begin():
        principal = Principal(kind="guest")
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"seat-auth-{uuid.uuid4()}",
            status="RUNNING",
        )
        session.add_all([principal, game])
        await session.flush()
        session.add_all(
            [
                GameSeat(
                    game_id=game.id,
                    public_player_id="P01",
                    seat_index=0,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value,
                    role="Villager",
                    faction="Town",
                    alive=True,
                    occupant_principal_id=principal.id,
                ),
                GameSeat(
                    game_id=game.id,
                    public_player_id="P02",
                    seat_index=1,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value,
                    role="Villager",
                    faction="Town",
                    alive=True,
                    occupant_principal_id=principal.id,
                ),
            ]
        )
        await session.flush()

        with pytest.raises(HTTPException) as exc_info:
            await resolve_human_game_seat(
                session,
                game_id=game.id,
                principal_id=principal.id,
                restrict_to_human_lane=True,
            )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "wrong_seat"
