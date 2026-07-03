"""Tests for the human-PII retention planner (US-308)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from padrino.db.retention import (
    GuestPrincipalRetentionInfo,
    HumanChatRetentionInfo,
    HumanConsentRetentionInfo,
    HumanPiiRetentionPolicy,
    plan_human_pii_retention,
)
from padrino.legal.documents import DEFAULT_RANKED_CHAT_RETENTION_TTL_DAYS
from padrino.settings import Settings

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
_POLICY = HumanPiiRetentionPolicy(
    chat_retention_ttl_days=30,
    consent_ip_hash_ttl_days=14,
    abandoned_guest_ttl_days=7,
)


def _chat(*, age: timedelta, redacted: bool = False) -> HumanChatRetentionInfo:
    return HumanChatRetentionInfo(
        id=uuid.uuid4(),
        created_at=_NOW - age,
        redacted=redacted,
    )


def _consent(
    *,
    age: timedelta,
    source_ip_hash: str | None = "a" * 64,
) -> HumanConsentRetentionInfo:
    return HumanConsentRetentionInfo(
        id=uuid.uuid4(),
        accepted_at=_NOW - age,
        source_ip_hash=source_ip_hash,
    )


def _guest(
    *,
    age: timedelta,
    kind: str = "guest",
    deleted: bool = False,
) -> GuestPrincipalRetentionInfo:
    return GuestPrincipalRetentionInfo(
        id=uuid.uuid4(),
        kind=kind,
        deleted_at=(_NOW - timedelta(days=1)) if deleted else None,
        idle_since=_NOW - age,
    )


def test_human_pii_planner_selects_only_strictly_past_ttl_rows() -> None:
    old_chat = _chat(age=timedelta(days=30, seconds=1))
    boundary_chat = _chat(age=timedelta(days=30))
    fresh_chat = _chat(age=timedelta(days=29, hours=23, minutes=59))
    redacted_chat = _chat(age=timedelta(days=45), redacted=True)

    old_consent = _consent(age=timedelta(days=14, seconds=1))
    boundary_consent = _consent(age=timedelta(days=14))
    fresh_consent = _consent(age=timedelta(days=13, hours=23, minutes=59))
    nulled_consent = _consent(age=timedelta(days=45), source_ip_hash=None)

    old_guest = _guest(age=timedelta(days=7, seconds=1))
    boundary_guest = _guest(age=timedelta(days=7))
    fresh_guest = _guest(age=timedelta(days=6, hours=23, minutes=59))
    account = _guest(age=timedelta(days=45), kind="account")
    deleted_guest = _guest(age=timedelta(days=45), deleted=True)

    plan = plan_human_pii_retention(
        chat_messages=[old_chat, boundary_chat, fresh_chat, redacted_chat],
        consents=[old_consent, boundary_consent, fresh_consent, nulled_consent],
        guest_principals=[old_guest, boundary_guest, fresh_guest, account, deleted_guest],
        policy=_POLICY,
        now=_NOW,
    )

    assert plan.chat_messages_to_redact == [old_chat.id]
    assert plan.consent_ip_hashes_to_null == [old_consent.id]
    assert plan.guest_principals_to_anonymize == [old_guest.id]


def test_human_pii_plan_defaults_to_dry_run_and_has_no_rating_delete_list() -> None:
    plan = plan_human_pii_retention(
        chat_messages=[],
        consents=[],
        guest_principals=[],
        policy=_POLICY,
        now=_NOW,
    )

    assert plan.dry_run is True
    assert all("rating" not in key for key in asdict(plan))


def test_human_pii_plan_dry_run_false_is_advisory_flag_only() -> None:
    plan = plan_human_pii_retention(
        chat_messages=[],
        consents=[],
        guest_principals=[],
        policy=_POLICY,
        now=_NOW,
        dry_run=False,
    )

    assert plan.dry_run is False


def test_human_pii_planner_has_no_wall_clock_or_random_reads() -> None:
    source = Path("src/padrino/db/retention.py").read_text(encoding="utf-8")

    assert "datetime.utcnow" not in source
    assert "datetime.now" not in source
    assert "import random" not in source
    assert "from random" not in source


def test_settings_drive_human_pii_ttls_and_chat_ttl_matches_ranked_consent_text() -> None:
    settings = Settings(_env_file=None)

    assert settings.padrino_human_chat_retention_ttl_days == (
        DEFAULT_RANKED_CHAT_RETENTION_TTL_DAYS
    )
    assert settings.padrino_human_consent_ip_hash_ttl_days == 90
    assert settings.padrino_abandoned_guest_ttl_days == 30


def test_env_overrides_human_pii_ttls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_HUMAN_CHAT_RETENTION_TTL_DAYS", "180")
    monkeypatch.setenv("PADRINO_HUMAN_CONSENT_IP_HASH_TTL_DAYS", "45")
    monkeypatch.setenv("PADRINO_ABANDONED_GUEST_TTL_DAYS", "14")
    settings = Settings(_env_file=None)

    assert settings.padrino_human_chat_retention_ttl_days == 180
    assert settings.padrino_human_consent_ip_hash_ttl_days == 45
    assert settings.padrino_abandoned_guest_ttl_days == 14
