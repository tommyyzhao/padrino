"""Retention planner for the compounding game bank (US-108).

Defines a pure planner function ``plan_retention`` that, given a sequence
of ``GameRetentionInfo`` objects and a ``RetentionPolicy``, returns a
``RetentionPlan`` identifying prune candidates.  The planner never touches
the database — callers (guarded jobs) execute the plan and default to a
safe ``dry_run=True``.

Retention rules
---------------
* ``game_events`` (public_event_v1), ``ratings``, ``rating_events``,
  ``analytics_aggregates``, and ``judge_enrichment_cards`` are kept forever.
* Heavy raw LLM-call payloads (``request_json`` + ``raw_response``) are
  **scrubbed** (nulled out) after ``raw_payload_ttl_days`` for ALL completed
  games, broadcastable or not.
* Non-broadcastable completed games are **hard-deleted** (game row + all
  cascades) after ``non_broadcastable_game_ttl_days``.  Broadcastable games
  are never hard-deleted by this policy, protecting ratings and replay data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RetentionPolicy:
    """Config-driven retention parameters."""

    raw_payload_ttl_days: int
    non_broadcastable_game_ttl_days: int


@dataclass(frozen=True)
class HumanPiiRetentionPolicy:
    """Config-driven human PII retention parameters."""

    chat_retention_ttl_days: int
    consent_ip_hash_ttl_days: int
    abandoned_guest_ttl_days: int


@dataclass(frozen=True)
class GameRetentionInfo:
    """Lightweight projection of a Game row needed by the planner.

    Callers fetch only these fields from the DB so the planner itself stays
    free of I/O.
    """

    id: uuid.UUID
    is_broadcastable: bool
    completed_at: datetime | None


@dataclass(frozen=True)
class HumanChatRetentionInfo:
    """Lightweight projection of one human chat sidecar row."""

    id: uuid.UUID
    created_at: datetime
    redacted: bool


@dataclass(frozen=True)
class HumanConsentRetentionInfo:
    """Lightweight projection of one human consent row."""

    id: uuid.UUID
    accepted_at: datetime
    source_ip_hash: str | None


@dataclass(frozen=True)
class GuestPrincipalRetentionInfo:
    """Lightweight projection of a principal's guest-retention state."""

    id: uuid.UUID
    kind: str
    deleted_at: datetime | None
    idle_since: datetime | None


@dataclass(frozen=True)
class OpenRouterPkceFlowRetentionInfo:
    """One short-lived OpenRouter connection flow eligible for deletion."""

    id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class RevokedCredentialRetentionInfo:
    """One revoked credential whose legacy envelope bytes may still need erasure."""

    id: uuid.UUID
    revoked_at: datetime
    ciphertext_present: bool
    nonce_present: bool


@dataclass
class RetentionPlan:
    """Output of ``plan_retention``: the set of changes a guarded job should apply.

    ``dry_run=True`` (the default) means the executor MUST NOT commit any
    destructive change — it should only log what *would* happen.
    """

    games_to_delete: list[uuid.UUID] = field(default_factory=list)
    llm_calls_to_scrub: list[uuid.UUID] = field(default_factory=list)
    dry_run: bool = True


@dataclass
class HumanPiiRetentionPlan:
    """Human PII rows that a guarded retention job should redact/anonymize."""

    chat_messages_to_redact: list[uuid.UUID] = field(default_factory=list)
    consent_ip_hashes_to_null: list[uuid.UUID] = field(default_factory=list)
    guest_principals_to_anonymize: list[uuid.UUID] = field(default_factory=list)
    pkce_flows_to_delete: list[uuid.UUID] = field(default_factory=list)
    revoked_credentials_to_erase: list[uuid.UUID] = field(default_factory=list)
    dry_run: bool = True


def plan_retention(
    games: Sequence[GameRetentionInfo],
    policy: RetentionPolicy,
    *,
    now: datetime,
    dry_run: bool = True,
) -> RetentionPlan:
    """Pure planner: decide which games and LLM-call rows need retention action.

    Parameters
    ----------
    games:
        Sequence of lightweight game projections to evaluate.
    policy:
        TTL configuration driving candidate selection.
    now:
        Current timestamp (injected so the function is deterministic and
        testable without wall-clock access).
    dry_run:
        When ``True`` (default) the returned plan is advisory only; the
        executor MUST NOT apply destructive changes.

    Returns
    -------
    RetentionPlan
        Candidates for deletion (non-broadcastable games past their TTL) and
        candidates for payload scrubbing (all completed games past the raw-
        payload TTL).  Broadcastable games are *never* placed in
        ``games_to_delete``, protecting ratings and public replay data.
    """
    now = _aware(now)

    games_to_delete: list[uuid.UUID] = []
    llm_calls_to_scrub: list[uuid.UUID] = []

    raw_cutoff = now - timedelta(days=policy.raw_payload_ttl_days)
    non_bc_cutoff = now - timedelta(days=policy.non_broadcastable_game_ttl_days)

    for g in games:
        if g.completed_at is None:
            continue

        completed = _aware(g.completed_at)

        if completed <= raw_cutoff:
            llm_calls_to_scrub.append(g.id)

        if not g.is_broadcastable and completed <= non_bc_cutoff:
            games_to_delete.append(g.id)

    # Remove games_to_delete entries from llm_calls_to_scrub — the whole game
    # row (including llm_calls via CASCADE) will be deleted anyway.
    delete_set = set(games_to_delete)
    llm_calls_to_scrub = [gid for gid in llm_calls_to_scrub if gid not in delete_set]

    return RetentionPlan(
        games_to_delete=games_to_delete,
        llm_calls_to_scrub=llm_calls_to_scrub,
        dry_run=dry_run,
    )


def plan_human_pii_retention(
    *,
    chat_messages: Sequence[HumanChatRetentionInfo],
    consents: Sequence[HumanConsentRetentionInfo],
    guest_principals: Sequence[GuestPrincipalRetentionInfo],
    policy: HumanPiiRetentionPolicy,
    now: datetime,
    dry_run: bool = True,
) -> HumanPiiRetentionPlan:
    """Pure planner for human PII retention.

    Human PII uses a stricter boundary than the original game-bank planner:
    rows are selected only when their timestamp is strictly older than the
    configured cutoff. Rows exactly at the cutoff remain within TTL.
    """
    now = _aware(now)
    chat_cutoff = now - timedelta(days=policy.chat_retention_ttl_days)
    consent_cutoff = now - timedelta(days=policy.consent_ip_hash_ttl_days)
    guest_cutoff = now - timedelta(days=policy.abandoned_guest_ttl_days)

    chat_messages_to_redact = [
        row.id for row in chat_messages if not row.redacted and _aware(row.created_at) < chat_cutoff
    ]
    consent_ip_hashes_to_null = [
        row.id
        for row in consents
        if row.source_ip_hash is not None and _aware(row.accepted_at) < consent_cutoff
    ]
    guest_principals_to_anonymize = [
        row.id
        for row in guest_principals
        if row.kind == "guest"
        and row.deleted_at is None
        and row.idle_since is not None
        and _aware(row.idle_since) < guest_cutoff
    ]

    return HumanPiiRetentionPlan(
        chat_messages_to_redact=chat_messages_to_redact,
        consent_ip_hashes_to_null=consent_ip_hashes_to_null,
        guest_principals_to_anonymize=guest_principals_to_anonymize,
        dry_run=dry_run,
    )


def plan_openrouter_credential_retention(
    *,
    flows: Sequence[OpenRouterPkceFlowRetentionInfo],
    revoked_credentials: Sequence[RevokedCredentialRetentionInfo],
    flow_retention_ttl_days: int,
    revoked_credential_retention_ttl_days: int,
    now: datetime,
) -> HumanPiiRetentionPlan:
    """Plan bounded cleanup of expired PKCE bindings and revoked envelopes.

    Both cutoffs are strict: values exactly at their configured retention
    boundary remain available for the full TTL.  Only an unexpected legacy
    envelope on an already-revoked row is selected for erasure.
    """
    now = _aware(now)
    flow_cutoff = now - timedelta(days=flow_retention_ttl_days)
    credential_cutoff = now - timedelta(days=revoked_credential_retention_ttl_days)
    return HumanPiiRetentionPlan(
        pkce_flows_to_delete=[row.id for row in flows if _aware(row.expires_at) < flow_cutoff],
        revoked_credentials_to_erase=[
            row.id
            for row in revoked_credentials
            if (row.ciphertext_present or row.nonce_present)
            and _aware(row.revoked_at) < credential_cutoff
        ],
    )
