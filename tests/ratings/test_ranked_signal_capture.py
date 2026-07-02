"""US-311: ranked human games capture best-effort anti-cheat telemetry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.human_consent import hash_source_ip
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameSeat, HumanRatingEvent, RankedGameSignal
from padrino.db.repositories import games, human_principals, leagues
from padrino.db.repositories import ranked_signals as ranked_signals_repo
from padrino.ratings.human_openskill_service import (
    HumanGameResult,
    update_human_ratings_for_game,
)

_GAME_SEED = "seed-ranked-signals-us311"
_NOW = datetime(2026, 6, 27, 13, 0, tzinfo=UTC)
_RAW_IP = "203.0.113.7"


def _rating_event_payload(event: HumanRatingEvent) -> dict[str, Any]:
    return {
        "league_id": event.league_id,
        "game_id": event.game_id,
        "human_player_id": event.human_player_id,
        "public_player_id": event.public_player_id,
        "scope_type": event.scope_type,
        "scope_value": event.scope_value,
        "before_mu": event.before_mu,
        "before_sigma": event.before_sigma,
        "after_mu": event.after_mu,
        "after_sigma": event.after_sigma,
    }


async def _seed_one_human_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    league_ranked: bool,
    source_ip_hash: str | None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    human = next(seat for seat in seats if seat.faction is Faction.TOWN)

    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=league_ranked,
        )
        game = await games.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"{_GAME_SEED}-{uuid.uuid4()}",
            status="COMPLETED",
        )
        game.ranked_source_ip_hash = source_ip_hash
        principal = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
            display_name=None,
        )
        for seat in seats:
            is_human = seat.public_player_id == human.public_player_id
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None,
                    seat_kind="HUMAN" if is_human else "AI",
                    occupant_principal_id=principal.id if is_human else None,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
    return league.id, game.id, principal.id, human.public_player_id, human.faction.value


@pytest.mark.asyncio
async def test_ranked_rating_write_captures_one_game_signal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ip_hash = hash_source_ip(_RAW_IP)
    assert ip_hash is not None
    league_id, game_id, principal_id, _public_id, faction = await _seed_one_human_game(
        session_factory,
        league_ranked=True,
        source_ip_hash=ip_hash,
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=_NOW,
        )

    assert len(events) == 1
    async with session_factory() as session:
        signal = (
            await session.execute(
                select(RankedGameSignal).where(RankedGameSignal.game_id == game_id)
            )
        ).scalar_one()
        game = await session.get(Game, game_id)

    assert signal.human_principal_id == principal_id
    assert signal.source_ip_hash == ip_hash
    assert signal.source_ip_hash != _RAW_IP
    assert len(signal.source_ip_hash) == 64
    assert signal.session_fingerprint_hash is None
    assert signal.winner == "TOWN"
    assert signal.faction == faction
    assert signal.signals == {}
    assert game is not None
    assert game.ranked_source_ip_hash == ip_hash
    assert game.ranked_source_ip_hash != _RAW_IP


@pytest.mark.asyncio
async def test_casual_game_writes_zero_ranked_game_signals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _principal_id, _public_id, _faction = await _seed_one_human_game(
        session_factory,
        league_ranked=False,
        source_ip_hash=hash_source_ip(_RAW_IP),
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=False,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=_NOW,
        )

    assert events == []
    async with session_factory() as session:
        signal_count = (
            await session.execute(select(func.count()).select_from(RankedGameSignal))
        ).scalar_one()
    assert signal_count == 0


@pytest.mark.asyncio
async def test_signal_capture_failure_never_alters_rating_event_payload(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ip_hash = hash_source_ip(_RAW_IP)
    assert ip_hash is not None
    league_id, game_id, _principal_id, _public_id, _faction = await _seed_one_human_game(
        session_factory,
        league_ranked=True,
        source_ip_hash=ip_hash,
    )

    async with session_factory() as session:
        enabled_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=_NOW,
        )
        enabled_payloads = [_rating_event_payload(event) for event in enabled_events]
        await session.rollback()

    async def fail_capture(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry store unavailable")

    monkeypatch.setattr(ranked_signals_repo, "record_ranked_game_signal", fail_capture)

    async with session_factory() as session, session.begin():
        failing_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=_NOW,
        )
        failing_payloads = [_rating_event_payload(event) for event in failing_events]

    assert failing_payloads == enabled_payloads
    async with session_factory() as session:
        signal_count = (
            await session.execute(select(func.count()).select_from(RankedGameSignal))
        ).scalar_one()
        rating_event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    assert signal_count == 0
    assert rating_event_count == 1


@pytest.mark.asyncio
async def test_signal_capture_db_fault_after_insert_rolls_back_signal_only(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ip_hash = hash_source_ip(_RAW_IP)
    assert ip_hash is not None
    league_id, game_id, _principal_id, _public_id, _faction = await _seed_one_human_game(
        session_factory,
        league_ranked=True,
        source_ip_hash=ip_hash,
    )

    original_execute = AsyncSession.execute
    faulted = False

    async def execute_then_fail_on_signal_insert(
        self: AsyncSession,
        statement: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal faulted
        result = await cast(Any, original_execute)(self, statement, *args, **kwargs)
        statement_text = str(statement).lower()
        if not faulted and "insert" in statement_text and "ranked_game_signals" in statement_text:
            faulted = True
            raise OperationalError(
                "INSERT ranked_game_signals",
                {},
                RuntimeError("simulated post-statement telemetry fault"),
            )
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_then_fail_on_signal_insert)

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=_NOW,
        )

    assert faulted is True
    assert len(events) == 1
    async with session_factory() as session:
        signal_count = (
            await session.execute(select(func.count()).select_from(RankedGameSignal))
        ).scalar_one()
        rating_event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    assert signal_count == 0
    assert rating_event_count == 1
