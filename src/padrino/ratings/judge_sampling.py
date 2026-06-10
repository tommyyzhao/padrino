"""Sampled batch judge enrichment job (US-105).

Selects a sample of completed games that have not yet been behaviorally evaluated,
runs the LLM judge on them, and aggregates per-agent/per-role trend cards into the
``JudgeEnrichmentCard`` enrichment table.

The enrichment table is **clearly separate** from the rating pipeline — this module
never writes to ``Rating`` or ``RatingEvent``.

Sampling semantics
------------------
From the pool of unevaluated COMPLETED games (most-recent-first), the batch job
takes ``ceil(n_candidates * padrino_judge_sample_rate)`` games, capped at
``padrino_judge_max_games_per_run``.  The most-recent-first ordering means the
ladder's top-tier games are evaluated first, which provides signal faster than
processing in chronological order.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.base import session_scope
from padrino.db.models import (
    BehavioralEvaluation,
    Game,
    GameSeat,
    JudgeEnrichmentCard,
)
from padrino.economics.spend_governor import can_start_game
from padrino.ratings.evaluator import JudgeAdapter, evaluate_completed_game_behavioral
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.ratings.judge_sampling")

# Upper bound on how many candidates we load from DB per run.
_CANDIDATE_POOL_SIZE: int = 200


async def _refresh_enrichment_cards(
    session: AsyncSession,
    agent_build_ids: set[uuid.UUID],
) -> None:
    """Recompute JudgeEnrichmentCard rows for *agent_build_ids* from all BehavioralEvaluation data."""
    for agent_build_id in agent_build_ids:
        stmt = (
            select(
                GameSeat.role,
                Game.ruleset_id,
                func.count(BehavioralEvaluation.id).label("cnt"),
                func.avg(BehavioralEvaluation.persuasion_score).label("avg_p"),
                func.avg(BehavioralEvaluation.deception_score).label("avg_d"),
                func.avg(BehavioralEvaluation.logical_consistency_score).label("avg_l"),
                func.avg(BehavioralEvaluation.social_heuristics_score).label("avg_s"),
            )
            .join(
                GameSeat,
                (GameSeat.game_id == BehavioralEvaluation.game_id)
                & (GameSeat.public_player_id == BehavioralEvaluation.public_player_id),
            )
            .join(Game, Game.id == BehavioralEvaluation.game_id)
            .where(BehavioralEvaluation.agent_build_id == agent_build_id)
            .group_by(GameSeat.role, Game.ruleset_id)
        )
        rows = (await session.execute(stmt)).all()

        for row in rows:
            role: str = row.role
            ruleset_id: str = row.ruleset_id
            cnt: int = int(row.cnt)
            avg_p: float = float(row.avg_p)
            avg_d: float = float(row.avg_d)
            avg_l: float = float(row.avg_l)
            avg_s: float = float(row.avg_s)

            existing_stmt = select(JudgeEnrichmentCard).where(
                JudgeEnrichmentCard.agent_build_id == agent_build_id,
                JudgeEnrichmentCard.role == role,
                JudgeEnrichmentCard.ruleset_id == ruleset_id,
            )
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()

            if existing is None:
                card = JudgeEnrichmentCard(
                    agent_build_id=agent_build_id,
                    role=role,
                    ruleset_id=ruleset_id,
                    games_count=cnt,
                    avg_persuasion=avg_p,
                    avg_deception=avg_d,
                    avg_logical_consistency=avg_l,
                    avg_social_heuristics=avg_s,
                )
                session.add(card)
            else:
                existing.games_count = cnt
                existing.avg_persuasion = avg_p
                existing.avg_deception = avg_d
                existing.avg_logical_consistency = avg_l
                existing.avg_social_heuristics = avg_s
                existing.computed_at = datetime.now(UTC)


async def run_sampled_judge_enrichment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    judge_adapter: JudgeAdapter | None = None,
) -> int:
    """Select a sampled batch of pending games, run the judge, and aggregate into enrichment cards.

    Respects the global spend governor (US-095): returns 0 immediately if the
    cumulative spend cap is reached.

    The per-run cap is ``padrino_judge_max_games_per_run`` (number of games
    processed per invocation).  Sampling fraction is ``padrino_judge_sample_rate``
    applied to the candidate pool.

    Returns the number of games successfully processed.
    """
    # Check global spend cap before doing any work.
    async with session_factory() as session:
        if not await can_start_game(session, settings):
            _logger.warning("judge_sampling.skipped", reason="spend_cap_reached")
            return 0

    # Load candidate pool: COMPLETED games without a BehavioralEvaluation, newest first.
    async with session_factory() as session:
        subq = select(BehavioralEvaluation.game_id).distinct()
        stmt = (
            select(Game.id)
            .where(Game.status == "COMPLETED", ~Game.id.in_(subq))
            .order_by(Game.created_at.desc())
            .limit(_CANDIDATE_POOL_SIZE)
        )
        result = await session.execute(stmt)
        candidates: list[uuid.UUID] = list(result.scalars())

    if not candidates:
        return 0

    # Apply sample rate and per-run cap to decide how many games to process.
    n_from_rate = math.ceil(len(candidates) * settings.padrino_judge_sample_rate)
    n_sample = min(settings.padrino_judge_max_games_per_run, n_from_rate)
    if n_sample <= 0:
        return 0

    sampled = candidates[:n_sample]

    # Evaluate each sampled game and collect affected agent_build_ids.
    processed_agent_build_ids: set[uuid.UUID] = set()
    n_processed = 0

    for game_id in sampled:
        try:
            async with session_scope(session_factory) as session:
                evals = await evaluate_completed_game_behavioral(session, game_id, judge_adapter)
                for ev in evals:
                    processed_agent_build_ids.add(ev.agent_build_id)
            n_processed += 1
            _logger.info("judge_sampling.game.evaluated", game_id=str(game_id))
        except Exception as exc:
            _logger.error("judge_sampling.game.failed", game_id=str(game_id), error=str(exc))

    # Aggregate evaluated scores into enrichment cards for all affected agents.
    if processed_agent_build_ids:
        async with session_scope(session_factory) as session:
            await _refresh_enrichment_cards(session, processed_agent_build_ids)

    _logger.info("judge_sampling.batch.done", n_processed=n_processed)
    return n_processed
