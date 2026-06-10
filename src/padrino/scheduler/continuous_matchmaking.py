"""Continuous matchmaking tick: admission → matchmaker → game runner → moderation gate (US-098).

On each scheduler tick :func:`run_continuous_matchmaking_tick`:
  1. Short-circuits (no-op) when ``padrino_enable_continuous_matchmaking`` is False.
  2. Checks ``admission.admit()``; logs and skips on denial (no partial games).
  3. Loads the curated roster (active AgentBuilds) and match history from the DB.
  4. Calls ``matchmaker.next_match()`` for a deterministic :class:`MatchPlan`.
  5. Executes one game via ``run_tournament_from_roster``.
  6. Runs the moderation gate (``is_broadcastable``) on the completed game.
  7. If broadcastable, marks ``is_broadcastable=True`` and transitions the game to LIVE.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import get_ruleset
from padrino.economics.admission import admit
from padrino.gauntlets.tournament import AdapterFactory, run_tournament_from_roster
from padrino.matchmaking.matchmaker import MatchRecord, next_match
from padrino.public.broadcast_index import mark_live
from padrino.public.moderation import GuardModelAdapter, is_broadcastable
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.scheduler.continuous_matchmaking")

# Fixed outer seed; gauntlet seed is derived as sha256("matchmaker:" + seed + ":" + history_len).
_OUTER_SEED = "continuous_matchmaking"
_RULESET_ID = "mini7_v1"


async def _load_roster(session: AsyncSession) -> list[uuid.UUID]:
    from padrino.db.models import AgentBuild as AgentBuildRow

    stmt = select(AgentBuildRow.id).where(AgentBuildRow.active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def _load_history(session: AsyncSession) -> list[MatchRecord]:
    from padrino.db.models import Game, GameSeat

    stmt = (
        select(Game.id, GameSeat.agent_build_id)
        .join(GameSeat, GameSeat.game_id == Game.id)
        .where(Game.status == "COMPLETED", Game.ruleset_id == _RULESET_ID)
    )
    rows = (await session.execute(stmt)).all()
    by_game: dict[uuid.UUID, list[uuid.UUID]] = {}
    for game_id, build_id in rows:
        by_game.setdefault(game_id, []).append(build_id)
    return [MatchRecord(participants=tuple(builds)) for builds in by_game.values()]


async def _load_league_id(session: AsyncSession) -> uuid.UUID | None:
    from padrino.db.models import League

    stmt = select(League.id).where(League.ruleset_id == _RULESET_ID).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_events_for_game(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from padrino.db.models import GameEvent

    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    return [
        {
            "event_type": ev.event_type,
            "phase": ev.phase,
            "payload": ev.payload,
        }
        for ev in (await session.execute(stmt)).scalars()
    ]


async def run_continuous_matchmaking_tick(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime,
    guard: GuardModelAdapter | None = None,
    adapter_factory: AdapterFactory | None = None,
) -> bool:
    """Run one continuous matchmaking tick. Returns True if a game was executed."""
    if not settings.padrino_enable_continuous_matchmaking:
        _logger.debug("continuous_matchmaking.disabled")
        return False

    async with session_factory() as session:
        decision = await admit(session, settings, now=now)

    if not decision.allowed:
        _logger.info("continuous_matchmaking.skipped", reason=decision.reason)
        return False

    async with session_factory() as session:
        roster = await _load_roster(session)
        history = await _load_history(session)
        league_id = await _load_league_id(session)

    if league_id is None:
        _logger.warning("continuous_matchmaking.no_league")
        return False

    ruleset = get_ruleset(_RULESET_ID)
    if len(roster) < ruleset.PLAYER_COUNT:
        _logger.warning(
            "continuous_matchmaking.roster_too_small",
            roster_size=len(roster),
            required=ruleset.PLAYER_COUNT,
        )
        return False

    plan = next_match(roster, history, ruleset_id=_RULESET_ID, seed=_OUTER_SEED)

    gauntlet_id, result = await run_tournament_from_roster(
        session_factory=session_factory,
        league_id=league_id,
        gauntlet_seed=plan.gauntlet_seed,
        roster_by_seat=plan.roster_by_seat,
        n_games=1,
        settings=settings,
        adapter_factory=adapter_factory,
    )

    if result.games_run == 0:
        _logger.info("continuous_matchmaking.no_games_ran")
        return False

    # Moderation gate: load events, check broadcastability, promote to LIVE.
    from padrino.db.models import Game

    async with session_factory() as session:
        stmt = select(Game.id).where(
            Game.gauntlet_id == gauntlet_id,
            Game.status == "COMPLETED",
        )
        game_ids = list((await session.execute(stmt)).scalars())

    for gid in game_ids:
        async with session_factory() as session, session.begin():
            events = await _load_events_for_game(session, gid)
            safe = await is_broadcastable(events, guard)
            if safe:
                game = await session.get(Game, gid)
                if game is not None:
                    game.is_broadcastable = True
                    # mark_live reuses the same identity-map instance; is_broadcastable=True
                    # is already set so the guard check inside mark_live passes.
                    await mark_live(session, gid)
                _logger.info("continuous_matchmaking.game_promoted", game_id=str(gid))
            else:
                _logger.info("continuous_matchmaking.game_not_broadcastable", game_id=str(gid))

    return True


__all__ = ["run_continuous_matchmaking_tick"]
