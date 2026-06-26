"""Repository helpers for benchmark campaigns and pairing ledger rows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.rulesets import get_ruleset
from padrino.core.scheduling.pairing import (
    PairingCell,
    PairingFormat,
    derive_campaign_cell_seed,
    generate_pairing_matrix,
)
from padrino.db.models import (
    AgentBuild,
    Campaign,
    CampaignPairing,
    Gauntlet,
    ModelConfig,
    PromptVersion,
)
from padrino.gauntlets.scheduler import create_paired_gauntlet

CAMPAIGN_STATUS_PENDING = "PENDING"
CAMPAIGN_STATUS_RUNNING = "RUNNING"
CAMPAIGN_STATUS_COMPLETED = "COMPLETED"

CAMPAIGN_PAIRING_PENDING = "PENDING"
CAMPAIGN_PAIRING_MATERIALIZED = "MATERIALIZED"
CAMPAIGN_PAIRING_COMPLETED = "COMPLETED"
CAMPAIGN_PAIRING_DEAD_LETTER = "DEAD_LETTER"
CAMPAIGN_PAIRING_TERMINAL_STATUSES = frozenset(
    {CAMPAIGN_PAIRING_COMPLETED, CAMPAIGN_PAIRING_DEAD_LETTER}
)


@dataclass(frozen=True, slots=True)
class CampaignCreated:
    """Persisted campaign id plus the model-id matrix that produced it."""

    campaign_id: uuid.UUID
    matrix: tuple[PairingCell, ...]


@dataclass(frozen=True, slots=True)
class MaterializedCampaignCell:
    """One campaign-pairing cell materialized into a child gauntlet."""

    cell_id: uuid.UUID
    cell_index: int
    gauntlet_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MaterializeBatchResult:
    """Result of one bounded campaign-ledger materialization pass."""

    campaign_id: uuid.UUID
    materialized: tuple[MaterializedCampaignCell, ...]


@dataclass(frozen=True, slots=True)
class DeadLetterCampaignCell:
    """Failure detail for one terminal campaign-pairing cell."""

    cell_id: uuid.UUID
    campaign_id: uuid.UUID
    cell_index: int
    gauntlet_id: uuid.UUID | None
    attempt_count: int
    last_error: str | None
    last_error_kind: str | None


@dataclass(frozen=True, slots=True)
class CampaignProgress:
    """Campaign-pairing terminal-cell progress as ``done of total``."""

    campaign_id: uuid.UUID
    done: int
    total: int


def _aware(value: datetime) -> datetime:
    """Treat SQLite's naive datetime reads as UTC for lease comparisons."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _format_cell_failure(*, last_error: str, last_error_kind: str | None) -> str:
    return last_error if last_error_kind is None else f"{last_error_kind}: {last_error}"


def _split_cell_failure(last_error: str | None) -> tuple[str | None, str | None]:
    if last_error is None:
        return None, None
    last_error_kind, separator, message = last_error.partition(": ")
    if not separator:
        return last_error, None
    return message, last_error_kind


async def get(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign | None:
    return await session.get(Campaign, campaign_id)


async def create_campaign_from_matrix(
    session: AsyncSession,
    *,
    campaign_seed: str,
    ruleset_id: str,
    league_id: uuid.UUID,
    model_field: list[str],
    format: PairingFormat | str,
    per_model_game_target: int,
    sigma_target: float,
    rank_stability_k: int,
    status: str = CAMPAIGN_STATUS_PENDING,
) -> CampaignCreated:
    """Create a campaign plus PENDING ledger cells, without child gauntlets.

    The pure pairing generator works in stable model ids. The persisted ledger
    stores resolved active ``agent_build_id`` UUID strings because paired
    gauntlet creation consumes builds, not model ids.
    """
    ruleset = get_ruleset(ruleset_id)
    pairing_format = PairingFormat(format)
    build_by_model, prompt_version_id = await _resolve_canonical_active_builds(
        session,
        model_field=model_field,
        ruleset_id=ruleset_id,
    )
    matrix = tuple(
        generate_pairing_matrix(
            campaign_seed,
            ruleset_id,
            model_field,
            format=pairing_format,
            per_model_game_target=per_model_game_target,
        )
    )
    campaign = Campaign(
        campaign_seed=campaign_seed,
        ruleset_id=ruleset_id,
        league_id=league_id,
        format=pairing_format.value,
        player_count=ruleset.PLAYER_COUNT,
        per_model_game_target=per_model_game_target,
        status=status,
        sigma_target=sigma_target,
        rank_stability_k=rank_stability_k,
    )
    session.add(campaign)
    await session.flush()

    for cell_index, roster in matrix:
        session.add(
            CampaignPairing(
                campaign_id=campaign.id,
                cell_index=cell_index,
                roster_json=[str(build_by_model[model_id].id) for model_id in roster],
                status=CAMPAIGN_PAIRING_PENDING,
            )
        )
    await session.flush()

    # The campaign schema intentionally does not duplicate prompt_version_id;
    # the selected prompt is recovered from resolved build rows at creation
    # time, and materialization verifies the stored rosters still agree.
    _ = prompt_version_id
    return CampaignCreated(campaign_id=campaign.id, matrix=matrix)


async def claim_campaign(
    session: AsyncSession,
    *,
    now: datetime,
    lease_ttl: timedelta,
    worker_id: str,
    exclude_campaign_ids: set[uuid.UUID] | None = None,
) -> Campaign | None:
    """Claim the oldest runnable campaign for one worker."""
    stmt = (
        select(Campaign)
        .where(
            or_(
                Campaign.status == CAMPAIGN_STATUS_PENDING,
                and_(
                    Campaign.status == CAMPAIGN_STATUS_RUNNING,
                    or_(
                        Campaign.lease_expires_at <= now,
                        Campaign.lease_expires_at.is_(None),
                    ),
                ),
            )
        )
        .order_by(Campaign.created_at, Campaign.id)
        .limit(1)
    )
    if exclude_campaign_ids:
        stmt = stmt.where(Campaign.id.notin_(exclude_campaign_ids))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    campaign = (await session.execute(stmt)).scalars().first()
    if campaign is None:
        return None
    campaign.status = CAMPAIGN_STATUS_RUNNING
    campaign.leased_by = worker_id
    campaign.lease_expires_at = now + lease_ttl
    campaign.heartbeat_at = now
    await session.flush()
    return campaign


async def claim_or_heartbeat_campaign(
    session: AsyncSession,
    *,
    now: datetime,
    lease_ttl: timedelta,
    worker_id: str,
    exclude_campaign_ids: set[uuid.UUID] | None = None,
) -> Campaign | None:
    """Return this worker's running campaign, or claim a new runnable one."""
    stmt = (
        select(Campaign)
        .where(
            Campaign.status == CAMPAIGN_STATUS_RUNNING,
            Campaign.leased_by == worker_id,
        )
        .order_by(Campaign.created_at, Campaign.id)
        .limit(1)
    )
    if exclude_campaign_ids:
        stmt = stmt.where(Campaign.id.notin_(exclude_campaign_ids))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    campaign = (await session.execute(stmt)).scalars().first()
    if campaign is None:
        return await claim_campaign(
            session,
            now=now,
            lease_ttl=lease_ttl,
            worker_id=worker_id,
            exclude_campaign_ids=exclude_campaign_ids,
        )
    campaign.heartbeat_at = now
    campaign.lease_expires_at = now + lease_ttl
    await session.flush()
    return campaign


async def update_heartbeat(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return
    campaign.heartbeat_at = now
    await session.flush()


async def pause_campaign(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> None:
    """Return a budget-paused non-terminal campaign to the pending queue."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.status == CAMPAIGN_STATUS_COMPLETED:
        return
    campaign.status = CAMPAIGN_STATUS_PENDING
    campaign.leased_by = None
    campaign.lease_expires_at = None
    campaign.heartbeat_at = None
    await session.flush()


async def reset_stale_campaigns(
    session: AsyncSession,
    *,
    now: datetime,
) -> list[uuid.UUID]:
    """Clear expired campaign leases and return campaign ids reset to PENDING."""
    stmt = (
        select(Campaign)
        .where(Campaign.status == CAMPAIGN_STATUS_RUNNING)
        .order_by(Campaign.created_at, Campaign.id)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    reset: list[uuid.UUID] = []
    cutoff = _aware(now)
    for campaign in rows:
        expires_at = campaign.lease_expires_at
        if expires_at is None or _aware(expires_at) <= cutoff:
            campaign.status = CAMPAIGN_STATUS_PENDING
            campaign.leased_by = None
            campaign.lease_expires_at = None
            campaign.heartbeat_at = None
            reset.append(campaign.id)
    if reset:
        await session.flush()
    return reset


async def materialize_next_batch(
    session: AsyncSession,
    *,
    campaign_id: uuid.UUID,
    batch_size: int,
    pair_count: int,
) -> MaterializeBatchResult:
    """Materialize up to ``batch_size`` PENDING campaign cells."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError(f"campaign {campaign_id} not found")

    stmt = (
        select(CampaignPairing)
        .where(
            CampaignPairing.campaign_id == campaign_id,
            CampaignPairing.status == CAMPAIGN_PAIRING_PENDING,
        )
        .order_by(CampaignPairing.cell_index)
        .limit(batch_size)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    cells = list((await session.execute(stmt)).scalars().all())
    materialized: list[MaterializedCampaignCell] = []
    for cell in cells:
        roster = [uuid.UUID(build_id) for build_id in cell.roster_json]
        prompt_version_id = await _prompt_version_for_roster(
            session,
            roster=roster,
            ruleset_id=campaign.ruleset_id,
        )
        created = await create_paired_gauntlet(
            session,
            league_id=campaign.league_id,
            ruleset_id=campaign.ruleset_id,
            prompt_version_id=prompt_version_id,
            pair_count=pair_count,
            gauntlet_seed=derive_campaign_cell_seed(campaign.campaign_seed, cell.cell_index),
            roster=roster,
            campaign_id=campaign.id,
        )
        cell.status = CAMPAIGN_PAIRING_MATERIALIZED
        cell.gauntlet_id = created.gauntlet_id
        materialized.append(
            MaterializedCampaignCell(
                cell_id=cell.id,
                cell_index=cell.cell_index,
                gauntlet_id=created.gauntlet_id,
            )
        )
    if materialized:
        await session.flush()
    return MaterializeBatchResult(campaign_id=campaign_id, materialized=tuple(materialized))


async def record_materialized_cell_failure(
    session: AsyncSession,
    *,
    gauntlet_id: uuid.UUID,
    last_error: str,
    last_error_kind: str | None,
    max_attempts: int,
    poison: bool,
) -> CampaignPairing | None:
    """Record a failed materialized-cell attempt and terminalize when exhausted."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    stmt = select(CampaignPairing).where(CampaignPairing.gauntlet_id == gauntlet_id).limit(1)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    cell = (await session.execute(stmt)).scalars().first()
    if cell is None:
        return None
    if cell.status == CAMPAIGN_PAIRING_DEAD_LETTER:
        return cell

    cell.attempt_count += 1
    cell.last_error = _format_cell_failure(
        last_error=last_error,
        last_error_kind=last_error_kind,
    )
    if poison or cell.attempt_count >= max_attempts:
        cell.status = CAMPAIGN_PAIRING_DEAD_LETTER
    else:
        cell.status = CAMPAIGN_PAIRING_PENDING
        cell.gauntlet_id = None
    await session.flush()
    return cell


async def list_dead_letter_cells(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> list[DeadLetterCampaignCell]:
    """Return campaign cell holes in deterministic cell-index order."""
    result = await session.execute(
        select(CampaignPairing)
        .where(
            CampaignPairing.campaign_id == campaign_id,
            CampaignPairing.status == CAMPAIGN_PAIRING_DEAD_LETTER,
        )
        .order_by(CampaignPairing.cell_index)
    )
    cells: list[DeadLetterCampaignCell] = []
    for cell in result.scalars():
        last_error, last_error_kind = _split_cell_failure(cell.last_error)
        cells.append(
            DeadLetterCampaignCell(
                cell_id=cell.id,
                campaign_id=cell.campaign_id,
                cell_index=cell.cell_index,
                gauntlet_id=cell.gauntlet_id,
                attempt_count=cell.attempt_count,
                last_error=last_error,
                last_error_kind=last_error_kind,
            )
        )
    return cells


async def campaign_progress(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> CampaignProgress | None:
    """Return terminal cell progress for one campaign, or ``None`` if unknown."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    statuses = list(
        (
            await session.execute(
                select(CampaignPairing.status).where(CampaignPairing.campaign_id == campaign_id)
            )
        )
        .scalars()
        .all()
    )
    return CampaignProgress(
        campaign_id=campaign_id,
        done=sum(1 for status in statuses if status in CAMPAIGN_PAIRING_TERMINAL_STATUSES),
        total=len(statuses),
    )


async def mark_materialized_cell_completed(
    session: AsyncSession,
    *,
    gauntlet_id: uuid.UUID,
) -> CampaignPairing | None:
    """Mark the campaign cell for ``gauntlet_id`` completed when one exists."""
    stmt = select(CampaignPairing).where(CampaignPairing.gauntlet_id == gauntlet_id).limit(1)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    cell = (await session.execute(stmt)).scalars().first()
    if cell is None:
        return None
    if cell.status in CAMPAIGN_PAIRING_TERMINAL_STATUSES:
        return cell
    if cell.status == CAMPAIGN_PAIRING_MATERIALIZED:
        cell.status = CAMPAIGN_PAIRING_COMPLETED
        await session.flush()
    return cell


async def reconcile_completed_materialized_cells(
    session: AsyncSession,
    *,
    campaign_id: uuid.UUID,
) -> tuple[CampaignPairing, ...]:
    """Complete materialized campaign cells whose child gauntlets already completed."""
    stmt = (
        select(CampaignPairing)
        .join(Gauntlet, CampaignPairing.gauntlet_id == Gauntlet.id)
        .where(
            CampaignPairing.campaign_id == campaign_id,
            CampaignPairing.status == CAMPAIGN_PAIRING_MATERIALIZED,
            Gauntlet.status == "COMPLETED",
        )
        .order_by(CampaignPairing.cell_index)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(of=CampaignPairing)
    cells = tuple((await session.execute(stmt)).scalars().all())
    for cell in cells:
        cell.status = CAMPAIGN_PAIRING_COMPLETED
    if cells:
        await session.flush()
    return cells


async def finalize_campaign_if_done(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    now: datetime,
) -> Campaign | None:
    """Auto-finalize a campaign once every pairing cell is terminal."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.status == CAMPAIGN_STATUS_COMPLETED:
        return None
    progress = await campaign_progress(session, campaign_id)
    if progress is None or progress.total == 0 or progress.done != progress.total:
        return None
    campaign.status = CAMPAIGN_STATUS_COMPLETED
    campaign.completed_at = now
    campaign.leased_by = None
    campaign.lease_expires_at = None
    campaign.heartbeat_at = None
    await session.flush()
    return campaign


async def _resolve_canonical_active_builds(
    session: AsyncSession,
    *,
    model_field: list[str],
    ruleset_id: str,
) -> tuple[dict[str, AgentBuild], uuid.UUID]:
    build_by_model: dict[str, AgentBuild] = {}
    for model_id in model_field:
        stmt = (
            select(AgentBuild)
            .join(ModelConfig, AgentBuild.model_config_id == ModelConfig.id)
            .where(
                AgentBuild.active.is_(True),
                or_(
                    ModelConfig.litellm_model_id == model_id,
                    ModelConfig.model_name == model_id,
                ),
            )
            .order_by(AgentBuild.created_at, AgentBuild.id)
            .limit(1)
        )
        build = (await session.execute(stmt)).scalars().first()
        if build is None:
            raise ValueError(f"no active agent_build found for model_id {model_id!r}")
        build_by_model[model_id] = build

    prompt_version_ids = {build.prompt_version_id for build in build_by_model.values()}
    if len(prompt_version_ids) != 1:
        raise ValueError("campaign model field resolves to multiple prompt_version_id values")
    prompt_version_id = next(iter(prompt_version_ids))
    prompt = await session.get(PromptVersion, prompt_version_id)
    if prompt is None:
        raise ValueError(f"prompt_version {prompt_version_id} not found")
    if prompt.ruleset_id != ruleset_id:
        raise ValueError(
            f"prompt_version {prompt_version_id} belongs to {prompt.ruleset_id!r}, "
            f"not {ruleset_id!r}"
        )
    return build_by_model, prompt_version_id


async def _prompt_version_for_roster(
    session: AsyncSession,
    *,
    roster: list[uuid.UUID],
    ruleset_id: str,
) -> uuid.UUID:
    stmt = select(AgentBuild).where(AgentBuild.id.in_(roster))
    builds = list((await session.execute(stmt)).scalars().all())
    by_id = {build.id: build for build in builds}
    missing = [build_id for build_id in roster if build_id not in by_id]
    if missing:
        raise ValueError(f"campaign roster references missing agent_build_id {missing[0]}")
    prompt_version_ids = {build.prompt_version_id for build in by_id.values()}
    if len(prompt_version_ids) != 1:
        raise ValueError("campaign roster references multiple prompt_version_id values")
    prompt_version_id = next(iter(prompt_version_ids))
    prompt = await session.get(PromptVersion, prompt_version_id)
    if prompt is None:
        raise ValueError(f"prompt_version {prompt_version_id} not found")
    if prompt.ruleset_id != ruleset_id:
        raise ValueError(
            f"prompt_version {prompt_version_id} belongs to {prompt.ruleset_id!r}, "
            f"not {ruleset_id!r}"
        )
    return prompt_version_id


__all__ = [
    "CAMPAIGN_PAIRING_COMPLETED",
    "CAMPAIGN_PAIRING_DEAD_LETTER",
    "CAMPAIGN_PAIRING_MATERIALIZED",
    "CAMPAIGN_PAIRING_PENDING",
    "CAMPAIGN_PAIRING_TERMINAL_STATUSES",
    "CAMPAIGN_STATUS_COMPLETED",
    "CAMPAIGN_STATUS_PENDING",
    "CAMPAIGN_STATUS_RUNNING",
    "CampaignCreated",
    "CampaignProgress",
    "DeadLetterCampaignCell",
    "MaterializeBatchResult",
    "MaterializedCampaignCell",
    "campaign_progress",
    "claim_campaign",
    "claim_or_heartbeat_campaign",
    "create_campaign_from_matrix",
    "finalize_campaign_if_done",
    "get",
    "list_dead_letter_cells",
    "mark_materialized_cell_completed",
    "materialize_next_batch",
    "pause_campaign",
    "reconcile_completed_materialized_cells",
    "record_materialized_cell_failure",
    "reset_stale_campaigns",
    "update_heartbeat",
]
