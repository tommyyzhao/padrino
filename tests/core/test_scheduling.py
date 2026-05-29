"""Unit tests for the pure-core cron helper (US-085)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from padrino.core.scheduling import humanize_cron, next_run_at


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_every_nth_minute() -> None:
    assert next_run_at("*/15 * * * *", after=_dt(2026, 5, 28, 12, 7)) == _dt(2026, 5, 28, 12, 15)
    # Strictly after: at an exact boundary it advances to the next slot.
    assert next_run_at("*/15 * * * *", after=_dt(2026, 5, 28, 12, 15)) == _dt(2026, 5, 28, 12, 30)


def test_every_nth_hour() -> None:
    assert next_run_at("0 */6 * * *", after=_dt(2026, 5, 28, 1, 0)) == _dt(2026, 5, 28, 6, 0)
    assert next_run_at("0 */6 * * *", after=_dt(2026, 5, 28, 6, 0)) == _dt(2026, 5, 28, 12, 0)


def test_month_boundary() -> None:
    # First-of-month from the last day of January rolls into February.
    assert next_run_at("0 0 1 * *", after=_dt(2026, 1, 31, 23, 0)) == _dt(2026, 2, 1, 0, 0)


def test_leap_day() -> None:
    # Feb 29 only exists in leap years; from 2025 the next is 2028.
    assert next_run_at("0 0 29 2 *", after=_dt(2025, 1, 1, 0, 0)) == _dt(2028, 2, 29, 0, 0)


def test_utc_daily_is_dst_stable() -> None:
    # US spring-forward is 2025-03-09. In UTC there is no DST jump, so a daily
    # 02:00 UTC schedule fires at 02:00 UTC on both sides of the civil change.
    assert next_run_at("0 2 * * *", after=_dt(2025, 3, 9, 1, 0)) == _dt(2025, 3, 9, 2, 0)
    assert next_run_at("0 2 * * *", after=_dt(2025, 3, 9, 2, 0)) == _dt(2025, 3, 10, 2, 0)


def test_dom_dow_or_semantics() -> None:
    # "0 0 13 * 5": midnight on the 13th OR any Friday. From Feb 1 2026 (a
    # Sunday) the first matching day is the first Friday, Feb 6 — proving the
    # OR rule (a non-13th Friday still matches). The 13th (also a Friday) and
    # subsequent Fridays follow.
    assert next_run_at("0 0 13 * 5", after=_dt(2026, 2, 1, 0, 0)) == _dt(2026, 2, 6, 0, 0)
    assert next_run_at("0 0 13 * 5", after=_dt(2026, 2, 13, 0, 0)) == _dt(2026, 2, 20, 0, 0)


def test_list_and_range_fields() -> None:
    assert next_run_at("0 9-17 * * 1-5", after=_dt(2026, 5, 29, 18, 0)) == _dt(2026, 6, 1, 9, 0)


def test_invalid_specs_raise() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        next_run_at("* * * *", after=_dt(2026, 1, 1))
    with pytest.raises(ValueError, match="out of range"):
        next_run_at("99 * * * *", after=_dt(2026, 1, 1))
    with pytest.raises(ValueError, match="non-numeric"):
        next_run_at("x * * * *", after=_dt(2026, 1, 1))


def test_humanize_cron() -> None:
    assert humanize_cron("* * * * *") == "every minute"
    assert humanize_cron("*/15 * * * *") == "every 15 minutes"
    assert humanize_cron("0 */6 * * *") == "every 6 hours"
    assert humanize_cron("0 2 * * *") == "every day at 02:00 UTC"
    assert humanize_cron("30 8 * * *") == "every day at 08:30 UTC"
    # Complex specs degrade to a generic label that never echoes the raw cron.
    human = humanize_cron("0 0 13 * 5")
    assert human == "custom schedule"
    assert "13" not in human


def test_humanize_never_leaks_raw_cron_on_garbage() -> None:
    assert humanize_cron("not a cron") == "custom schedule"
