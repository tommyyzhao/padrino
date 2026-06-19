"""Retention executor: applies the US-108 retention planner (US-116).

The pure planner in :mod:`padrino.db.retention` decides *which* games to
hard-delete and *which* games' heavy LLM-call payloads to scrub.  This module
is the impure executor that the planner was designed for: it loads the
candidate game projections from the database, runs :func:`plan_retention`, and
(when explicitly enabled and not in dry-run) applies the plan inside a
transaction.

Safety posture
--------------
* Both ``padrino_enable_retention`` and an explicit flip of
  ``padrino_retention_dry_run`` to ``False`` are required before a single row
  is mutated.  The dry-run default logs the plan and touches nothing.
* Only **non-broadcastable** completed games past their TTL are hard-deleted
  (the planner never returns broadcastable games).  Ratings, rating events and
  public replay data therefore can never be deleted by this job.
* Scrubbing nulls only the heavy payload columns (``request_json``,
  ``raw_response``) on ``llm_calls`` past the raw-payload TTL; cost and token
  metrics are preserved for spend accounting.
* Deletion removes child rows (seats, events, llm_calls, rating_events,
  behavioral_evaluations) explicitly before the game row so the job is portable
  across SQLite (which does not enforce FK cascade for the non-cascade FKs) and
  PostgreSQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    BehavioralEvaluation,
    Game,
    GameEvent,
    GameSeat,
    LlmCall,
    RatingEvent,
)
from padrino.db.retention import (
    GameRetentionInfo,
    RetentionPlan,
    RetentionPolicy,
    plan_retention,
)
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.db.retention_executor")


# Empty JSON payload written into scrubbed ``request_json`` (column is NOT NULL).
_SCRUBBED_REQUEST_JSON: dict[str, object] = {}


@dataclass(frozen=True)
class RetentionExecutionResult:
    """Outcome of one retention executor run."""

    dry_run: bool
    games_deleted: int
    llm_calls_scrubbed: int


async def _load_candidates(session: AsyncSession) -> list[GameRetentionInfo]:
    """Project completed games into the lightweight planner input."""
    stmt = select(Game.id, Game.is_broadcastable, Game.completed_at).where(
        Game.completed_at.is_not(None)
    )
    rows = (await session.execute(stmt)).all()
    return [
        GameRetentionInfo(
            id=row.id,
            is_broadcastable=row.is_broadcastable,
            completed_at=row.completed_at,
        )
        for row in rows
    ]


async def _delete_game(session: AsyncSession, game_id: uuid.UUID) -> None:
    """Hard-delete a game and all rows referencing it (children first)."""
    await session.execute(delete(LlmCall).where(LlmCall.game_id == game_id))
    await session.execute(
        delete(BehavioralEvaluation).where(BehavioralEvaluation.game_id == game_id)
    )
    await session.execute(delete(RatingEvent).where(RatingEvent.game_id == game_id))
    await session.execute(delete(GameEvent).where(GameEvent.game_id == game_id))
    await session.execute(delete(GameSeat).where(GameSeat.game_id == game_id))
    await session.execute(delete(Game).where(Game.id == game_id))


async def _scrub_llm_calls(session: AsyncSession, game_id: uuid.UUID) -> int:
    """Null the heavy payload columns on a game's llm_calls; return rows touched.

    Idempotent: rows already scrubbed (``raw_response IS NULL``) are not
    re-counted, so a re-run reports zero further scrubs.
    """
    count_stmt = (
        select(func.count())
        .select_from(LlmCall)
        .where(LlmCall.game_id == game_id, LlmCall.raw_response.is_not(None))
    )
    affected = int((await session.execute(count_stmt)).scalar_one())
    if affected == 0:
        return 0

    update_stmt = (
        update(LlmCall)
        .where(LlmCall.game_id == game_id, LlmCall.raw_response.is_not(None))
        .values(request_json=_SCRUBBED_REQUEST_JSON, raw_response=None)
    )
    await session.execute(update_stmt)
    return affected


async def run_retention_executor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime,
) -> RetentionExecutionResult | None:
    """Run one retention pass. Returns ``None`` when retention is disabled.

    Parameters
    ----------
    session_factory:
        Async session factory (impure DB boundary).
    settings:
        Supplies the TTL policy and the two guard flags.
    now:
        Injected wall-clock (the planner is deterministic given ``now``).
    """
    if not settings.padrino_enable_retention:
        _logger.debug("retention.disabled")
        return None

    dry_run = settings.padrino_retention_dry_run
    policy = RetentionPolicy(
        raw_payload_ttl_days=settings.padrino_raw_payload_ttl_days,
        non_broadcastable_game_ttl_days=settings.padrino_non_broadcastable_game_ttl_days,
    )

    async with session_factory() as session:
        candidates = await _load_candidates(session)

    plan: RetentionPlan = plan_retention(candidates, policy, now=now, dry_run=dry_run)

    if dry_run:
        _logger.info(
            "retention.dry_run",
            games_to_delete=len(plan.games_to_delete),
            llm_calls_to_scrub=len(plan.llm_calls_to_scrub),
        )
        return RetentionExecutionResult(
            dry_run=True,
            games_deleted=0,
            llm_calls_scrubbed=0,
        )

    scrubbed_total = 0
    async with session_factory() as session, session.begin():
        for game_id in plan.games_to_delete:
            await _delete_game(session, game_id)
        for game_id in plan.llm_calls_to_scrub:
            scrubbed_total += await _scrub_llm_calls(session, game_id)

    _logger.info(
        "retention.applied",
        games_deleted=len(plan.games_to_delete),
        llm_calls_scrubbed=scrubbed_total,
    )
    return RetentionExecutionResult(
        dry_run=False,
        games_deleted=len(plan.games_to_delete),
        llm_calls_scrubbed=scrubbed_total,
    )


__all__ = ["RetentionExecutionResult", "run_retention_executor"]
