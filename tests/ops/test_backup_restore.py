"""US-242: backup restore hash-chain verification."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.conftest import make_town_win_script

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.game_status import GAME_STATUS_COMPLETED, GAME_STATUS_RUNNING
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import games as games_repo
from padrino.llm.mock import DeterministicMockAdapter
from padrino.ops.backup_restore import verify_restored_game_hash_chain
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game

_GAME_SEED = "seed-us242-backup-restore"


def _script_for_seed() -> dict[tuple[str, str], Any]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return make_town_win_script(
        mafia_ids=mafia,
        town_ids=town,
        doctor_id=doctor,
        detective_id=detective,
    )


async def _run_finished_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status=GAME_STATUS_RUNNING,
        )
        game_id = game.id

    await run_game(
        GameConfig(game_id="G-US242", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(_script_for_seed()),
        ranked=False,
        persistence=GamePersistence(session_factory=session_factory, game_id=game_id),
    )
    return game_id


async def _dump_finished_game_events(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> dict[str, Any]:
    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        assert game.status == GAME_STATUS_COMPLETED
        stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
        rows = list((await session.execute(stmt)).scalars())
        assert rows
        return {
            "game": {
                "id": str(game.id),
                "ruleset_id": game.ruleset_id,
                "game_seed": game.game_seed,
                "status": game.status,
                "terminal_result": game.terminal_result,
                "current_phase": game.current_phase,
                "event_hash_head": game.event_hash_head,
            },
            "events": [
                {
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "phase": row.phase,
                    "visibility": row.visibility,
                    "actor_player_id": row.actor_player_id,
                    "payload": row.payload,
                    "prev_event_hash": row.prev_event_hash,
                    "event_hash": row.event_hash,
                }
                for row in rows
            ],
        }


async def _restore_backup_to_fresh_db(
    db_path: Path,
    backup: dict[str, Any],
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    game_raw = backup["game"]
    game_id = uuid.UUID(game_raw["id"])

    async with session_factory() as session, session.begin():
        session.add(
            Game(
                id=game_id,
                gauntlet_id=None,
                ruleset_id=game_raw["ruleset_id"],
                game_seed=game_raw["game_seed"],
                status=game_raw["status"],
                terminal_result=game_raw["terminal_result"],
                current_phase=game_raw["current_phase"],
                event_hash_head=game_raw["event_hash_head"],
            )
        )
        for raw in backup["events"]:
            session.add(
                GameEvent(
                    game_id=game_id,
                    sequence=raw["sequence"],
                    event_type=raw["event_type"],
                    phase=raw["phase"],
                    visibility=raw["visibility"],
                    actor_player_id=raw["actor_player_id"],
                    payload=raw["payload"],
                    prev_event_hash=raw["prev_event_hash"],
                    event_hash=raw["event_hash"],
                )
            )
    return engine, session_factory


async def test_restored_backup_reverifies_finished_game_hash_chain(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    game_id = await _run_finished_game(session_factory)
    backup = await _dump_finished_game_events(session_factory, game_id)
    backup_path = tmp_path / "finished-game-events-backup.json"
    backup_path.write_text(json.dumps(backup, sort_keys=True), encoding="utf-8")

    restored_backup = json.loads(backup_path.read_text(encoding="utf-8"))
    restore_engine, restore_factory = await _restore_backup_to_fresh_db(
        tmp_path / "restore.sqlite",
        restored_backup,
    )
    try:
        async with restore_factory() as session:
            verification = await verify_restored_game_hash_chain(session, game_id)
    finally:
        await restore_engine.dispose()

    assert verification.event_count == len(backup["events"])
    assert verification.final_event_type == "GameTerminated"
    assert verification.tip_hash == backup["events"][-1]["event_hash"]
    assert verification.tip_hash == backup["game"]["event_hash_head"]
