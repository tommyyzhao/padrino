"""US-301: admin ranked rating rollback endpoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_ADMIN, SCOPE_SPECTATOR, generate_raw_key
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    GameEvent,
    GameSeat,
    HumanRating,
    HumanRatingEvent,
    RankedGameExclusion,
    RankedRatingRollbackAudit,
)
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games, human_principals, leagues
from padrino.ratings.human_openskill_service import (
    HumanGameResult,
    update_human_ratings_for_game,
)

_NOW = datetime(2026, 6, 27, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _api_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
) -> tuple[str, uuid.UUID]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        row = await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label="test")
        return raw, row.id


def _headers(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _principal(
    session: AsyncSession,
    *,
    label: str,
) -> uuid.UUID:
    principal = await human_principals.create_principal(
        session,
        kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
        display_name=label,
    )
    return principal.id


async def _seed_ranked_game(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_seed: str,
    principal_id: uuid.UUID,
) -> uuid.UUID:
    seats = assign_roles(game_seed, mini7_v1)
    target_seat = next(seat for seat in seats if seat.faction is Faction.TOWN)
    game = await games.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=game_seed,
        status="COMPLETED",
    )
    game.terminal_result = {"winner": "TOWN", "reason": "test"}
    for seat in seats:
        is_target = seat.public_player_id == target_seat.public_player_id
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None,
                seat_kind="HUMAN" if is_target else "AI",
                occupant_principal_id=principal_id if is_target else None,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
            )
        )
    log = EventLog()
    for event_type, payload in (
        ("GameCreated", {"game_id": str(game.id), "ruleset_id": mini7_v1.RULESET_ID}),
        ("GameTerminated", {"winner": "TOWN", "reason": "test"}),
    ):
        stored = log.append(
            {
                "event_type": event_type,
                "sequence": len(log.events),
                "phase": "TERMINAL" if event_type == "GameTerminated" else "SETUP",
                "visibility": "PUBLIC",
                "actor_player_id": None,
                "payload": payload,
            }
        )
        await events_repo.append_event(
            session,
            game_id=game.id,
            sequence=stored.sequence,
            event_type=stored.body["event_type"],
            phase=stored.body["phase"],
            visibility=stored.body["visibility"],
            actor_player_id=stored.body["actor_player_id"],
            payload=stored.body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )
    await update_human_ratings_for_game(
        session,
        league_id=league_id,
        ranked=True,
        rating_enabled=True,
        game_result=HumanGameResult(game_id=game.id, winner="TOWN"),
        now=_NOW,
    )
    return game.id


async def _seed_ranked_sequence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    principal_label: str,
    game_seeds: tuple[str, ...],
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...], uuid.UUID]:
    async with session_factory() as session, session.begin():
        league = await leagues.get_or_create_humans_included(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        principal_id = await _principal(session, label=principal_label)
        game_ids: list[uuid.UUID] = []
        for game_seed in game_seeds:
            game_ids.append(
                await _seed_ranked_game(
                    session,
                    league_id=league.id,
                    game_seed=game_seed,
                    principal_id=principal_id,
                )
            )
        return league.id, tuple(game_ids), principal_id


async def _rating_for(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
) -> HumanRating:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanRating).where(HumanRating.human_player_id == str(principal_id))
            )
        ).scalar_one()
        session.expunge(row)
        return row


async def _event_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> list[tuple[object, ...]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
            )
        ).scalars()
        return [
            (
                row.id,
                row.game_id,
                row.sequence,
                row.event_type,
                row.phase,
                row.visibility,
                row.actor_player_id,
                row.payload,
                row.prev_event_hash,
                row.event_hash,
            )
            for row in rows
        ]


@pytest.mark.asyncio
async def test_admin_rollback_excludes_games_recomputes_rating_and_preserves_event_log(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_raw, admin_id = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    _league_id, game_ids, principal_id = await _seed_ranked_sequence(
        session_factory,
        principal_label="rollback-target",
        game_seeds=("rollback-game-1", "rollback-game-2", "rollback-game-3"),
    )
    _fresh_league_id, _fresh_game_ids, fresh_principal_id = await _seed_ranked_sequence(
        session_factory,
        principal_label="fresh-target",
        game_seeds=("rollback-game-1", "rollback-game-3"),
    )
    before_events = await _event_snapshot(session_factory, game_ids[1])

    response = await client.post(
        f"/admin/anticheat/principals/{principal_id}/rollback",
        headers=_headers(admin_raw),
        json={
            "reason": "confirmed_cheating",
            "excluded_game_ids": [str(game_ids[1])],
        },
    )

    assert response.status_code == 200, response.text
    rolled_back = await _rating_for(session_factory, principal_id)
    fresh = await _rating_for(session_factory, fresh_principal_id)
    assert rolled_back.games == 2
    assert rolled_back.mu == pytest.approx(fresh.mu)
    assert rolled_back.sigma == pytest.approx(fresh.sigma)
    assert rolled_back.conservative_score == pytest.approx(fresh.conservative_score)
    assert await _event_snapshot(session_factory, game_ids[1]) == before_events

    async with session_factory() as session:
        exclusion_rows = (await session.execute(select(RankedGameExclusion))).scalars().all()
        audit_rows = (await session.execute(select(RankedRatingRollbackAudit))).scalars().all()
        rating_event_count = (
            await session.execute(
                select(func.count())
                .select_from(HumanRatingEvent)
                .where(HumanRatingEvent.human_player_id == str(principal_id))
            )
        ).scalar_one()

    assert rating_event_count == 3
    assert len(exclusion_rows) == 1
    assert exclusion_rows[0].principal_id == principal_id
    assert exclusion_rows[0].human_player_id == str(principal_id)
    assert exclusion_rows[0].game_id == game_ids[1]
    assert exclusion_rows[0].reason == "confirmed_cheating"
    assert exclusion_rows[0].created_by == str(admin_id)
    assert len(audit_rows) == 1
    assert audit_rows[0].principal_id == principal_id
    assert audit_rows[0].human_player_id == str(principal_id)
    assert audit_rows[0].reason == "confirmed_cheating"
    assert audit_rows[0].excluded_game_ids == [str(game_ids[1])]
    assert audit_rows[0].created_by == str(admin_id)

    response_2 = await client.post(
        f"/admin/anticheat/principals/{principal_id}/rollback",
        headers=_headers(admin_raw),
        json={
            "reason": "confirmed_cheating",
            "excluded_game_ids": [str(game_ids[1])],
        },
    )

    assert response_2.status_code == 200, response_2.text
    rolled_back_again = await _rating_for(session_factory, principal_id)
    assert rolled_back_again.mu == pytest.approx(rolled_back.mu)
    assert rolled_back_again.sigma == pytest.approx(rolled_back.sigma)
    assert rolled_back_again.games == rolled_back.games
    async with session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(RankedGameExclusion))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(RankedRatingRollbackAudit))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_admin_rollback_requires_admin_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    spectator_raw, _spectator_id = await _api_key(session_factory, scopes=[SCOPE_SPECTATOR])
    _league_id, game_ids, principal_id = await _seed_ranked_sequence(
        session_factory,
        principal_label="rollback-non-admin",
        game_seeds=("non-admin-game-1",),
    )

    response = await client.post(
        f"/admin/anticheat/principals/{principal_id}/rollback",
        headers=_headers(spectator_raw),
        json={
            "reason": "confirmed_cheating",
            "excluded_game_ids": [str(game_ids[0])],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_scope"


def test_rollback_route_is_owned_by_existing_admin_router() -> None:
    admin_source = (_REPO_ROOT / "src" / "padrino" / "api" / "routes" / "admin.py").read_text()
    anticheat_source = (
        _REPO_ROOT / "src" / "padrino" / "api" / "routes" / "admin_anticheat.py"
    ).read_text()

    assert "/admin/anticheat/principals/{principal_id}/rollback" in admin_source
    assert "admin_anticheat" not in admin_source
    assert "rollback" not in anticheat_source
