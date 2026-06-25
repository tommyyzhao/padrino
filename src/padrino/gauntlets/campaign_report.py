"""Campaign reporting for cost, progress, ETA, and convergence."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import (
    AgentBuild,
    Campaign,
    CampaignPairing,
    Game,
    Gauntlet,
    LlmCall,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import campaigns as campaigns_repo
from padrino.ratings.model_rollup import rollup_by_model
from padrino.ratings.openskill_service import SCOPE_FACTION, SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.provisional_and_decay import DEFAULT_PROVISIONAL_GAMES, is_provisional

_ScopeKind = Literal["ruleset", "faction", "model"]


class CampaignReportNotFound(ValueError):
    """Raised when a report is requested for an unknown campaign."""


class CampaignProgressReport(BaseModel):
    """Status counts for a campaign pairing ledger."""

    model_config = ConfigDict(frozen=True)

    done_cells: int
    total_cells: int
    remaining_cells: int
    pending: int
    materialized: int
    completed: int
    dead_letter: int


class CampaignEtaReport(BaseModel):
    """Simple remaining-work estimate derived from observed campaign cells."""

    model_config = ConfigDict(frozen=True)

    remaining_cells: int
    observed_cells: int
    observed_avg_cost_per_cell_usd: float | None
    estimated_remaining_cost_usd: float | None
    observed_avg_duration_seconds: float | None
    estimated_remaining_seconds: float | None


class CampaignDeadLetterReport(BaseModel):
    """One documented campaign-pairing hole."""

    model_config = ConfigDict(frozen=True)

    cell_id: uuid.UUID
    cell_index: int
    gauntlet_id: uuid.UUID | None
    attempt_count: int
    last_error: str | None
    last_error_kind: str | None


class CampaignRankStability(BaseModel):
    """Rank movement over a recent rating-update window."""

    model_config = ConfigDict(frozen=True)

    window_size: int
    observed_updates: int
    current_rank: int | None
    earliest_rank: int | None
    rank_delta: int | None
    stable: bool | None


class CampaignConvergenceItem(BaseModel):
    """One per-scope convergence row used by operator reports and publish gates."""

    model_config = ConfigDict(frozen=True)

    scope_kind: _ScopeKind
    entity_id: str
    entity_label: str
    scope_type: str
    scope_value: str
    games: int
    mu: float
    sigma: float
    conservative_score: float
    provisional: bool
    rank: int | None
    rank_stability: CampaignRankStability


class CampaignReport(BaseModel):
    """Typed campaign report with a JSON-stable shape."""

    model_config = ConfigDict(frozen=True)

    campaign_id: uuid.UUID
    league_id: uuid.UUID
    ruleset_id: str
    status: str
    rank_stability_k: int
    total_cost_usd: float
    progress: CampaignProgressReport
    eta: CampaignEtaReport
    dead_letters: tuple[CampaignDeadLetterReport, ...]
    convergence: tuple[CampaignConvergenceItem, ...]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ranks(scores: Mapping[str, float]) -> dict[str, int]:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {entity_id: rank for rank, (entity_id, _score) in enumerate(ordered, start=1)}


def _score(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


async def build_campaign_report(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    provisional_games_threshold: int = DEFAULT_PROVISIONAL_GAMES,
) -> CampaignReport:
    """Build the campaign operator report for one persisted campaign."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignReportNotFound(f"campaign {campaign_id} not found")

    total_cost_usd = await _total_campaign_cost_usd(session, campaign_id)
    progress = await _campaign_progress_report(session, campaign_id)
    eta = await _campaign_eta_report(
        session,
        campaign_id,
        total_cost_usd=total_cost_usd,
        progress=progress,
    )
    dead_letters = tuple(
        CampaignDeadLetterReport(
            cell_id=item.cell_id,
            cell_index=item.cell_index,
            gauntlet_id=item.gauntlet_id,
            attempt_count=item.attempt_count,
            last_error=item.last_error,
            last_error_kind=item.last_error_kind,
        )
        for item in await campaigns_repo.list_dead_letter_cells(session, campaign_id)
    )
    convergence = await _campaign_convergence(
        session,
        campaign=campaign,
        provisional_games_threshold=provisional_games_threshold,
    )
    return CampaignReport(
        campaign_id=campaign.id,
        league_id=campaign.league_id,
        ruleset_id=campaign.ruleset_id,
        status=campaign.status,
        rank_stability_k=campaign.rank_stability_k,
        total_cost_usd=total_cost_usd,
        progress=progress,
        eta=eta,
        dead_letters=dead_letters,
        convergence=convergence,
    )


async def _campaign_progress_report(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> CampaignProgressReport:
    rows = (
        await session.execute(
            select(CampaignPairing.status, func.count(CampaignPairing.id))
            .where(CampaignPairing.campaign_id == campaign_id)
            .group_by(CampaignPairing.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    pending = counts.get(campaigns_repo.CAMPAIGN_PAIRING_PENDING, 0)
    materialized = counts.get(campaigns_repo.CAMPAIGN_PAIRING_MATERIALIZED, 0)
    completed = counts.get(campaigns_repo.CAMPAIGN_PAIRING_COMPLETED, 0)
    dead_letter = counts.get(campaigns_repo.CAMPAIGN_PAIRING_DEAD_LETTER, 0)
    total = pending + materialized + completed + dead_letter
    done = completed + dead_letter
    return CampaignProgressReport(
        done_cells=done,
        total_cells=total,
        remaining_cells=max(0, total - done),
        pending=pending,
        materialized=materialized,
        completed=completed,
        dead_letter=dead_letter,
    )


async def _total_campaign_cost_usd(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> float:
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
        .select_from(LlmCall)
        .join(Game, Game.id == LlmCall.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(Gauntlet.campaign_id == campaign_id)
    )
    return float(total or 0.0)


async def _campaign_eta_report(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    total_cost_usd: float,
    progress: CampaignProgressReport,
) -> CampaignEtaReport:
    remaining = progress.remaining_cells
    observed_avg_cost = total_cost_usd / progress.done_cells if progress.done_cells > 0 else None
    durations = await _completed_cell_durations_seconds(session, campaign_id)
    observed_avg_duration = (sum(durations) / len(durations)) if durations else None
    return CampaignEtaReport(
        remaining_cells=remaining,
        observed_cells=progress.done_cells,
        observed_avg_cost_per_cell_usd=observed_avg_cost,
        estimated_remaining_cost_usd=(
            observed_avg_cost * remaining if observed_avg_cost is not None else None
        ),
        observed_avg_duration_seconds=observed_avg_duration,
        estimated_remaining_seconds=(
            observed_avg_duration * remaining if observed_avg_duration is not None else None
        ),
    )


async def _completed_cell_durations_seconds(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> list[float]:
    rows = (
        await session.execute(
            select(
                CampaignPairing.id,
                func.min(Game.created_at),
                func.max(Game.completed_at),
            )
            .select_from(CampaignPairing)
            .join(Gauntlet, Gauntlet.id == CampaignPairing.gauntlet_id)
            .join(Game, Game.gauntlet_id == Gauntlet.id)
            .where(
                CampaignPairing.campaign_id == campaign_id,
                CampaignPairing.status == campaigns_repo.CAMPAIGN_PAIRING_COMPLETED,
                Game.completed_at.is_not(None),
            )
            .group_by(CampaignPairing.id)
        )
    ).all()
    durations: list[float] = []
    for _cell_id, started_at, completed_at in rows:
        if started_at is None or completed_at is None:
            continue
        durations.append(max(0.0, (_aware(completed_at) - _aware(started_at)).total_seconds()))
    return durations


async def _campaign_convergence(
    session: AsyncSession,
    *,
    campaign: Campaign,
    provisional_games_threshold: int,
) -> tuple[CampaignConvergenceItem, ...]:
    ratings = await _campaign_ratings(session, campaign)
    entities = await _agent_entities(session, (rating.agent_build_id for rating in ratings))
    items: list[CampaignConvergenceItem] = []
    for (scope_type, scope_value), scoped_ratings in _group_ratings(ratings).items():
        scores = {str(r.agent_build_id): float(r.conservative_score) for r in scoped_ratings}
        ranks = _ranks(scores)
        scope_kind: _ScopeKind = "faction" if scope_type == SCOPE_FACTION else "ruleset"
        for rating in scoped_ratings:
            entity_id = str(rating.agent_build_id)
            stability = await _agent_rank_stability(
                session,
                campaign=campaign,
                scope_type=scope_type,
                scope_value=scope_value,
                entity_id=entity_id,
                current_scores=scores,
                current_rank=ranks.get(entity_id),
            )
            items.append(
                CampaignConvergenceItem(
                    scope_kind=scope_kind,
                    entity_id=entity_id,
                    entity_label=entities.get(rating.agent_build_id, entity_id),
                    scope_type=scope_type,
                    scope_value=scope_value,
                    games=int(rating.games),
                    mu=float(rating.mu),
                    sigma=float(rating.sigma),
                    conservative_score=float(rating.conservative_score),
                    provisional=is_provisional(
                        int(rating.games), threshold=provisional_games_threshold
                    ),
                    rank=ranks.get(entity_id),
                    rank_stability=stability,
                )
            )

    model_items = await _model_convergence(
        session,
        campaign=campaign,
        provisional_games_threshold=provisional_games_threshold,
    )
    items.extend(model_items)
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.scope_kind,
                item.scope_type,
                item.scope_value,
                item.rank is None,
                item.rank if item.rank is not None else 10**9,
                item.entity_label,
                item.entity_id,
            ),
        )
    )


async def _campaign_ratings(
    session: AsyncSession,
    campaign: Campaign,
) -> list[Rating]:
    stmt = (
        select(Rating)
        .where(
            Rating.league_id == campaign.league_id,
            or_(Rating.ruleset_id == campaign.ruleset_id, Rating.ruleset_id.is_(None)),
            Rating.scope_type.in_((SCOPE_GLOBAL, SCOPE_FACTION)),
        )
        .order_by(Rating.scope_type, Rating.scope_value, Rating.agent_build_id)
    )
    return list((await session.execute(stmt)).scalars().all())


def _group_ratings(ratings: Iterable[Rating]) -> dict[tuple[str, str], list[Rating]]:
    grouped: dict[tuple[str, str], list[Rating]] = {}
    for rating in ratings:
        grouped.setdefault((rating.scope_type, rating.scope_value), []).append(rating)
    return grouped


async def _agent_entities(
    session: AsyncSession,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, str]:
    ids = tuple(set(agent_build_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(AgentBuild.id, AgentBuild.display_name).where(AgentBuild.id.in_(ids))
        )
    ).all()
    return {ab_id: str(display_name) for ab_id, display_name in rows}


async def _agent_rank_stability(
    session: AsyncSession,
    *,
    campaign: Campaign,
    scope_type: str,
    scope_value: str,
    entity_id: str,
    current_scores: Mapping[str, float],
    current_rank: int | None,
) -> CampaignRankStability:
    events = await _recent_rating_events(
        session,
        campaign=campaign,
        scope_type=scope_type,
        scope_value=scope_value,
        limit=campaign.rank_stability_k,
    )
    baseline = dict(current_scores)
    for event in events:
        baseline[str(event.agent_build_id)] = _score(
            float(event.before_mu), float(event.before_sigma)
        )
    earliest_rank = _ranks(baseline).get(entity_id)
    rank_delta = (
        abs(current_rank - earliest_rank)
        if current_rank is not None and earliest_rank is not None
        else None
    )
    return CampaignRankStability(
        window_size=campaign.rank_stability_k,
        observed_updates=len(events),
        current_rank=current_rank,
        earliest_rank=earliest_rank,
        rank_delta=rank_delta,
        stable=(rank_delta == 0) if rank_delta is not None else None,
    )


async def _recent_rating_events(
    session: AsyncSession,
    *,
    campaign: Campaign,
    scope_type: str,
    scope_value: str,
    limit: int,
) -> list[RatingEvent]:
    if limit <= 0:
        return []
    stmt = (
        select(RatingEvent)
        .where(
            RatingEvent.league_id == campaign.league_id,
            RatingEvent.ruleset_id == campaign.ruleset_id,
            RatingEvent.scope_type == scope_type,
            RatingEvent.scope_value == scope_value,
        )
        .order_by(desc(RatingEvent.created_at), desc(RatingEvent.id))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _model_convergence(
    session: AsyncSession,
    *,
    campaign: Campaign,
    provisional_games_threshold: int,
) -> tuple[CampaignConvergenceItem, ...]:
    rollup = await rollup_by_model(session, campaign.league_id, campaign.ruleset_id)
    if not rollup.entries:
        return ()
    scores = {entry.model_key: float(entry.conservative_score) for entry in rollup.entries}
    ranks = _ranks(scores)
    stability_by_model = await _model_rank_stability(
        session,
        campaign=campaign,
        current_scores=scores,
        model_build_ids={entry.model_key: entry.agent_build_ids for entry in rollup.entries},
    )
    return tuple(
        CampaignConvergenceItem(
            scope_kind="model",
            entity_id=entry.model_key,
            entity_label=entry.display_name,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            games=entry.games,
            mu=entry.mu,
            sigma=entry.sigma,
            conservative_score=entry.conservative_score,
            provisional=is_provisional(entry.games, threshold=provisional_games_threshold),
            rank=ranks.get(entry.model_key),
            rank_stability=stability_by_model[entry.model_key],
        )
        for entry in rollup.entries
    )


async def _model_rank_stability(
    session: AsyncSession,
    *,
    campaign: Campaign,
    current_scores: Mapping[str, float],
    model_build_ids: Mapping[str, tuple[uuid.UUID, ...]],
) -> dict[str, CampaignRankStability]:
    current_ranks = _ranks(current_scores)
    events = await _recent_rating_events(
        session,
        campaign=campaign,
        scope_type=SCOPE_GLOBAL,
        scope_value=SCOPE_VALUE_GLOBAL,
        limit=campaign.rank_stability_k,
    )
    build_to_model = {
        build_id: model_key
        for model_key, build_ids in model_build_ids.items()
        for build_id in build_ids
    }
    baseline = dict(current_scores)
    for event in events:
        model_key = build_to_model.get(event.agent_build_id)
        if model_key is None:
            continue
        after = _score(float(event.after_mu), float(event.after_sigma))
        before = _score(float(event.before_mu), float(event.before_sigma))
        baseline[model_key] = baseline.get(model_key, 0.0) - (after - before)
    earliest_ranks = _ranks(baseline)
    out: dict[str, CampaignRankStability] = {}
    for model_key in current_scores:
        current_rank = current_ranks.get(model_key)
        earliest_rank = earliest_ranks.get(model_key)
        rank_delta = (
            abs(current_rank - earliest_rank)
            if current_rank is not None and earliest_rank is not None
            else None
        )
        out[model_key] = CampaignRankStability(
            window_size=campaign.rank_stability_k,
            observed_updates=len(events),
            current_rank=current_rank,
            earliest_rank=earliest_rank,
            rank_delta=rank_delta,
            stable=(rank_delta == 0) if rank_delta is not None else None,
        )
    return out


def render_campaign_report_summary(report: CampaignReport) -> str:
    """Render a compact human-readable campaign report."""
    progress = report.progress
    eta = report.eta
    eta_seconds = (
        "unknown"
        if eta.estimated_remaining_seconds is None
        else f"{eta.estimated_remaining_seconds:.0f}s"
    )
    remaining_cost = (
        "unknown"
        if eta.estimated_remaining_cost_usd is None
        else f"${eta.estimated_remaining_cost_usd:.4f}"
    )
    return "\n".join(
        [
            (
                f"Campaign {report.campaign_id} "
                f"ruleset={report.ruleset_id} status={report.status} "
                f"cost=${report.total_cost_usd:.4f}"
            ),
            (
                f"Progress {progress.done_cells}/{progress.total_cells} cells "
                f"(PENDING={progress.pending}, MATERIALIZED={progress.materialized}, "
                f"COMPLETED={progress.completed}, DEAD_LETTER={progress.dead_letter})"
            ),
            (
                f"ETA remaining_cells={eta.remaining_cells} "
                f"remaining_cost={remaining_cost} remaining_time={eta_seconds}"
            ),
            f"Dead letters={len(report.dead_letters)} convergence_scopes={len(report.convergence)}",
        ]
    )


__all__ = [
    "CampaignConvergenceItem",
    "CampaignDeadLetterReport",
    "CampaignEtaReport",
    "CampaignProgressReport",
    "CampaignRankStability",
    "CampaignReport",
    "CampaignReportNotFound",
    "build_campaign_report",
    "render_campaign_report_summary",
]
