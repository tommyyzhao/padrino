"""Campaign scheduler tick orchestration.

The campaign tick is intentionally small: each scheduler tick reaps expired
campaign leases, claims or heartbeats one campaign for this worker, and
materializes a bounded number of pending campaign cells into ordinary
gauntlets. The existing gauntlet scheduler loop remains the only game runner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.repositories import campaigns as campaigns_repo
from padrino.economics.benchmark_admission import (
    release_benchmark_budget_reservation,
    reserve_benchmark_budget,
)
from padrino.settings import Settings

_CAMPAIGN_PAIR_COUNT = 1
_GLOBAL_BUDGET_DENIED = "global_budget_cap_reached"


@dataclass(frozen=True, slots=True)
class CampaignTickResult:
    """Summary of one campaign scheduler tick."""

    reset_campaign_ids: tuple[uuid.UUID, ...]
    campaign_id: uuid.UUID | None
    materialized: tuple[campaigns_repo.MaterializedCampaignCell, ...]
    finalized_campaign_id: uuid.UUID | None


def _empty_result(*, reset_campaign_ids: tuple[uuid.UUID, ...] = ()) -> CampaignTickResult:
    return CampaignTickResult(
        reset_campaign_ids=reset_campaign_ids,
        campaign_id=None,
        materialized=(),
        finalized_campaign_id=None,
    )


async def run_campaign_tick(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    settings: Settings,
    worker_id: str,
) -> CampaignTickResult:
    """Run one bounded campaign tick using the scheduler's injected clock."""
    if not settings.padrino_enable_campaign_tick:
        return _empty_result()
    if settings.padrino_campaign_materialize_batch_size <= 0:
        raise ValueError("padrino_campaign_materialize_batch_size must be > 0")
    if settings.padrino_campaign_lease_ttl_seconds <= 0:
        raise ValueError("padrino_campaign_lease_ttl_seconds must be > 0")

    async with session_factory() as session, session.begin():
        reset_campaign_ids = tuple(await campaigns_repo.reset_stale_campaigns(session, now=now))

    skipped_campaign_ids: set[uuid.UUID] = set()
    while True:
        async with session_factory() as session, session.begin():
            campaign = await campaigns_repo.claim_or_heartbeat_campaign(
                session,
                now=now,
                lease_ttl=timedelta(seconds=settings.padrino_campaign_lease_ttl_seconds),
                worker_id=worker_id,
                exclude_campaign_ids=skipped_campaign_ids,
            )
            if campaign is None:
                return _empty_result(reset_campaign_ids=reset_campaign_ids)

            decision = await reserve_benchmark_budget(
                session,
                settings,
                now=now,
                binding_key=f"campaign-tick:{campaign.id}",
                campaign_id=campaign.id,
            )
            if not decision.allowed:
                await campaigns_repo.pause_campaign(session, campaign.id)
                if decision.reason == _GLOBAL_BUDGET_DENIED:
                    return _empty_result(reset_campaign_ids=reset_campaign_ids)
                skipped_campaign_ids.add(campaign.id)
                continue

            assert decision.reservation is not None
            batch = await campaigns_repo.materialize_next_batch(
                session,
                campaign_id=campaign.id,
                batch_size=settings.padrino_campaign_materialize_batch_size,
                pair_count=_CAMPAIGN_PAIR_COUNT,
            )
            await release_benchmark_budget_reservation(
                session,
                decision.reservation,
                released_at=now,
            )
            finalized = await campaigns_repo.finalize_campaign_if_done(
                session,
                campaign.id,
                now=now,
            )
            return CampaignTickResult(
                reset_campaign_ids=reset_campaign_ids,
                campaign_id=campaign.id,
                materialized=batch.materialized,
                finalized_campaign_id=finalized.id if finalized is not None else None,
            )


__all__ = ["CampaignTickResult", "run_campaign_tick"]
