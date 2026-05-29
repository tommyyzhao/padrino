"""Pure-core 5-field cron evaluation for scheduled gauntlets (US-085).

`next_run_at(cron_expr, *, after)` returns the next firing time strictly after
``after`` for a standard 5-field cron spec (minute hour day-of-month month
day-of-week). It is deterministic and reads no wall-clock — ``after`` is
injected — so it stays inside the pure-core firewall and replays bit-for-bit.

Supported field syntax per the classic Vixie cron grammar:

* ``*``              — every value
* ``*/n``            — every n-th value from the field minimum
* ``a``              — a single value
* ``a-b``            — an inclusive range
* ``a-b/n``          — a stepped range
* ``a,b,c``          — a comma list of any of the above

Day-of-week is ``0-6`` with Sunday = 0 (``7`` is also accepted as Sunday).
When BOTH day-of-month and day-of-week are restricted (neither is ``*``) a day
matches if EITHER field matches — the standard cron OR rule. When only one is
restricted the other is ignored.

Timezone note: matching is done against the calendar fields of ``after`` and
the helper steps in absolute minutes. Padrino's scheduler runs in UTC, where
there are no DST transitions, so a spec like ``0 2 * * *`` fires at 02:00 UTC
every day, stably. Civil-timezone DST semantics are intentionally out of scope
(the scheduler never passes a DST-observing ``after``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Safety horizon: the largest realistic gap between firings is "every Feb 29",
# whose worst case spans up to 8 years (e.g. 2096 -> 2104, since 2100 is not a
# leap year). Cap a little above that so an impossible spec raises instead of
# looping forever.
_MAX_MINUTES_AHEAD = 9 * 366 * 24 * 60


@dataclass(frozen=True, slots=True)
class _Field:
    values: frozenset[int]
    wildcard: bool


def _parse_field(spec: str, lo: int, hi: int) -> _Field:
    wildcard = spec == "*"
    values: set[int] = set()
    for chunk in spec.split(","):
        if not chunk:
            raise ValueError(f"empty cron field component in {spec!r}")
        body, _, step_s = chunk.partition("/")
        step = 1
        if _:
            if not step_s.isdigit() or int(step_s) <= 0:
                raise ValueError(f"invalid step in cron component {chunk!r}")
            step = int(step_s)
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            start_s, _sep, end_s = body.partition("-")
            start, end = _as_int(start_s), _as_int(end_s)
        else:
            start = end = _as_int(body)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron component {chunk!r} out of range [{lo}, {hi}]")
        values.update(v for v in range(start, end + 1) if (v - start) % step == 0)
    return _Field(values=frozenset(values), wildcard=wildcard)


def _as_int(token: str) -> int:
    if not token.isdigit():
        raise ValueError(f"non-numeric cron token {token!r}")
    return int(token)


@dataclass(frozen=True, slots=True)
class _CronSpec:
    minute: _Field
    hour: _Field
    dom: _Field
    month: _Field
    dow: _Field


def _parse(cron_expr: str) -> _CronSpec:
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(parts)}: {cron_expr!r}")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow_raw = _parse_field(parts[4], 0, 7)
    # Normalize 7 -> 0 (both mean Sunday).
    dow = _Field(
        values=frozenset(0 if v == 7 else v for v in dow_raw.values),
        wildcard=dow_raw.wildcard,
    )
    return _CronSpec(minute=minute, hour=hour, dom=dom, month=month, dow=dow)


def _day_matches(spec: _CronSpec, moment: datetime) -> bool:
    # Cron weekday: Sunday=0..Saturday=6. Python weekday(): Monday=0..Sunday=6.
    cron_dow = (moment.weekday() + 1) % 7
    dom_hit = moment.day in spec.dom.values
    dow_hit = cron_dow in spec.dow.values
    if not spec.dom.wildcard and not spec.dow.wildcard:
        return dom_hit or dow_hit
    if not spec.dom.wildcard:
        return dom_hit
    if not spec.dow.wildcard:
        return dow_hit
    return True


def _matches(spec: _CronSpec, moment: datetime) -> bool:
    return (
        moment.minute in spec.minute.values
        and moment.hour in spec.hour.values
        and moment.month in spec.month.values
        and _day_matches(spec, moment)
    )


def next_run_at(cron_expr: str, *, after: datetime) -> datetime:
    """Return the next firing time strictly after ``after`` for ``cron_expr``.

    Raises ``ValueError`` on a malformed spec or one with no firing within the
    safety horizon (e.g. an impossible day/month combination).
    """
    spec = _parse(cron_expr)
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(_MAX_MINUTES_AHEAD):
        if _matches(spec, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"cron expression {cron_expr!r} has no firing within the horizon")


def humanize_cron(cron_expr: str) -> str:
    """Return a human-readable description, never echoing the raw cron expr.

    Covers the common shapes (every minute / every N minutes / every N hours /
    daily at HH:MM UTC). Anything else degrades to a generic label so the
    public payload never leaks the precise schedule.
    """
    try:
        parts = cron_expr.split()
        if len(parts) != 5:
            return "custom schedule"
        minute, hour, dom, month, dow = parts
        if (minute, hour, dom, month, dow) == ("*", "*", "*", "*", "*"):
            return "every minute"
        if hour == dom == month == dow == "*" and minute.startswith("*/"):
            return f"every {minute[2:]} minutes"
        if minute == "0" and dom == month == dow == "*" and hour.startswith("*/"):
            return f"every {hour[2:]} hours"
        if dom == month == dow == "*" and minute.isdigit() and hour.isdigit():
            return f"every day at {int(hour):02d}:{int(minute):02d} UTC"
    except (ValueError, IndexError):
        return "custom schedule"
    return "custom schedule"


__all__ = ["humanize_cron", "next_run_at"]
