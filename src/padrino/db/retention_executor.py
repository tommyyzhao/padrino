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
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import (
    BehavioralEvaluation,
    Game,
    GameEvent,
    GameSeat,
    HumanChatMessage,
    HumanConsent,
    HumanSession,
    LlmCall,
    Principal,
    RatingEvent,
)
from padrino.db.repositories import human_chat as human_chat_repo
from padrino.db.retention import (
    GameRetentionInfo,
    GuestPrincipalRetentionInfo,
    HumanChatRetentionInfo,
    HumanConsentRetentionInfo,
    HumanPiiRetentionPlan,
    HumanPiiRetentionPolicy,
    RetentionPlan,
    RetentionPolicy,
    plan_human_pii_retention,
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
    human_chat_messages_redacted: int = 0
    human_consent_ip_hashes_nulled: int = 0
    guest_principals_anonymized: int = 0


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


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime from SQLite to UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _latest_datetime(values: list[datetime | None]) -> datetime | None:
    """Return the latest non-null timestamp, normalizing naive values to UTC."""
    normalized = [_aware(value) for value in values if value is not None]
    return max(normalized) if normalized else None


async def _load_human_chat_candidates(session: AsyncSession) -> list[HumanChatRetentionInfo]:
    """Project human chat sidecar rows into the PII planner input."""
    stmt = select(HumanChatMessage.id, HumanChatMessage.created_at, HumanChatMessage.redacted)
    rows = (await session.execute(stmt)).all()
    return [
        HumanChatRetentionInfo(
            id=row.id,
            created_at=row.created_at,
            redacted=row.redacted,
        )
        for row in rows
    ]


async def _load_human_consent_candidates(
    session: AsyncSession,
) -> list[HumanConsentRetentionInfo]:
    """Project consent rows into the PII planner input."""
    stmt = select(HumanConsent.id, HumanConsent.accepted_at, HumanConsent.source_ip_hash)
    rows = (await session.execute(stmt)).all()
    return [
        HumanConsentRetentionInfo(
            id=row.id,
            accepted_at=row.accepted_at,
            source_ip_hash=row.source_ip_hash,
        )
        for row in rows
    ]


async def _load_guest_principal_candidates(
    session: AsyncSession,
) -> list[GuestPrincipalRetentionInfo]:
    """Project guest principals with their latest session activity."""
    stmt = (
        select(
            Principal.id,
            Principal.kind,
            Principal.deleted_at,
            Principal.created_at,
            Principal.updated_at,
            HumanSession.issued_at,
            HumanSession.last_seen_at,
        )
        .outerjoin(HumanSession, HumanSession.principal_id == Principal.id)
        .where(Principal.kind == "guest")
    )
    grouped: dict[uuid.UUID, GuestPrincipalRetentionInfo] = {}
    for row in (await session.execute(stmt)).all():
        prior = grouped.get(row.id)
        idle_since = _latest_datetime(
            [
                row.created_at,
                row.updated_at,
                row.last_seen_at,
                row.issued_at,
                prior.idle_since if prior is not None else None,
            ]
        )
        grouped[row.id] = GuestPrincipalRetentionInfo(
            id=row.id,
            kind=row.kind,
            deleted_at=row.deleted_at,
            idle_since=idle_since,
        )
    return list(grouped.values())


async def _load_human_pii_candidates(
    session: AsyncSession,
) -> tuple[
    list[HumanChatRetentionInfo],
    list[HumanConsentRetentionInfo],
    list[GuestPrincipalRetentionInfo],
]:
    """Project every human-PII row type needed by the pure planner."""
    return (
        await _load_human_chat_candidates(session),
        await _load_human_consent_candidates(session),
        await _load_guest_principal_candidates(session),
    )


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


async def _redact_human_chat_messages(
    session: AsyncSession,
    message_ids: list[uuid.UUID],
) -> int:
    """Redact planned human chat sidecar rows, idempotently."""
    if not message_ids:
        return 0
    rows = list(
        (
            await session.execute(
                select(HumanChatMessage).where(
                    HumanChatMessage.id.in_(message_ids),
                    HumanChatMessage.redacted.is_(False),
                )
            )
        ).scalars()
    )
    for row in rows:
        row.raw_text = None
        row.cleaned_text = None
        row.redacted = True
    if rows:
        await session.flush()
    return len(rows)


async def _null_human_consent_ip_hashes(
    session: AsyncSession,
    consent_ids: list[uuid.UUID],
) -> int:
    """Null planned consent source-IP hashes, idempotently."""
    if not consent_ids:
        return 0
    rows = list(
        (
            await session.execute(
                select(HumanConsent).where(
                    HumanConsent.id.in_(consent_ids),
                    HumanConsent.source_ip_hash.is_not(None),
                )
            )
        ).scalars()
    )
    for row in rows:
        row.source_ip_hash = None
    if rows:
        await session.flush()
    return len(rows)


async def _anonymize_guest_principals(
    session: AsyncSession,
    principal_ids: list[uuid.UUID],
    *,
    now: datetime,
) -> tuple[int, int]:
    """Soft-delete planned abandoned guests and redact their authored chat."""
    if not principal_ids:
        return 0, 0
    rows = list(
        (
            await session.execute(
                select(Principal).where(
                    Principal.id.in_(principal_ids),
                    Principal.kind == "guest",
                    Principal.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    redacted_chat_total = 0
    for row in rows:
        redacted_chat_total += await human_chat_repo.redact_for_principal(
            session,
            principal_id=row.id,
        )
        row.deleted_at = now
        row.display_name = None
        row.ranked_handle = None
        row.ranked_handle_normalized = None
        row.updated_at = now
    if rows:
        await session.flush()
    return len(rows), redacted_chat_total


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
    human_policy = HumanPiiRetentionPolicy(
        chat_retention_ttl_days=settings.padrino_human_chat_retention_ttl_days,
        consent_ip_hash_ttl_days=settings.padrino_human_consent_ip_hash_ttl_days,
        abandoned_guest_ttl_days=settings.padrino_abandoned_guest_ttl_days,
    )

    async with session_factory() as session:
        candidates = await _load_candidates(session)
        chat_candidates, consent_candidates, guest_candidates = await _load_human_pii_candidates(
            session
        )

    plan: RetentionPlan = plan_retention(candidates, policy, now=now, dry_run=dry_run)
    human_plan: HumanPiiRetentionPlan = plan_human_pii_retention(
        chat_messages=chat_candidates,
        consents=consent_candidates,
        guest_principals=guest_candidates,
        policy=human_policy,
        now=now,
        dry_run=dry_run,
    )

    if dry_run:
        _logger.info(
            "retention.dry_run",
            games_to_delete=len(plan.games_to_delete),
            llm_calls_to_scrub=len(plan.llm_calls_to_scrub),
            human_chat_messages_to_redact=len(human_plan.chat_messages_to_redact),
            human_consent_ip_hashes_to_null=len(human_plan.consent_ip_hashes_to_null),
            guest_principals_to_anonymize=len(human_plan.guest_principals_to_anonymize),
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
        redacted_total = await _redact_human_chat_messages(
            session,
            human_plan.chat_messages_to_redact,
        )
        nulled_total = await _null_human_consent_ip_hashes(
            session,
            human_plan.consent_ip_hashes_to_null,
        )
        anonymized_total, guest_chat_redacted_total = await _anonymize_guest_principals(
            session,
            human_plan.guest_principals_to_anonymize,
            now=now,
        )
        redacted_total += guest_chat_redacted_total

    _logger.info(
        "retention.applied",
        games_deleted=len(plan.games_to_delete),
        llm_calls_scrubbed=scrubbed_total,
        human_chat_messages_redacted=redacted_total,
        human_consent_ip_hashes_nulled=nulled_total,
        guest_principals_anonymized=anonymized_total,
    )
    return RetentionExecutionResult(
        dry_run=False,
        games_deleted=len(plan.games_to_delete),
        llm_calls_scrubbed=scrubbed_total,
        human_chat_messages_redacted=redacted_total,
        human_consent_ip_hashes_nulled=nulled_total,
        guest_principals_anonymized=anonymized_total,
    )


__all__ = ["RetentionExecutionResult", "run_retention_executor"]
