"""Batch/admin-triggered ranked anti-cheat scoring.

This read-side service derives advisory flags from ``ranked_game_signals`` and
persists only ``ranked_flags`` rows. It is intentionally separate from the
ranked human-rating write path and never mutates ``human_rating`` or
``human_rating_event`` rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.anticheat.signals import (
    RankedSignalRow,
    RankedSignalThresholds,
)
from padrino.core.anticheat.signals import (
    score_ranked_signals as score_ranked_signal_rows,
)
from padrino.db.models import RankedFlag, RankedGameSignal
from padrino.db.repositories import ranked_flags as ranked_flags_repo
from padrino.settings import Settings


def thresholds_from_settings(settings: Settings) -> RankedSignalThresholds:
    """Build pure scorer thresholds from operator Settings."""
    return RankedSignalThresholds(
        window_seconds=settings.padrino_ranked_flags_window_days * 24 * 60 * 60,
        same_ip_min_principals=settings.padrino_ranked_flags_source_ip_min_principals,
        cooccurrence_min_games=settings.padrino_ranked_flags_cooccurrence_min_games,
        streak_min_games=settings.padrino_ranked_flags_streak_min_games,
        streak_max_tail_probability=settings.padrino_ranked_flags_streak_max_tail_probability,
        bot_cadence_min_games=settings.padrino_ranked_flags_bot_cadence_min_games,
        bot_cadence_max_average_latency_ms=(
            settings.padrino_ranked_flags_bot_cadence_max_average_latency_ms
        ),
        bot_cadence_max_latency_spread_ms=(
            settings.padrino_ranked_flags_bot_cadence_max_latency_spread_ms
        ),
    )


async def score_ranked_flags(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime,
) -> list[RankedFlag]:
    """Score existing ranked telemetry and persist newly discovered flags."""
    stmt = select(RankedGameSignal).order_by(
        RankedGameSignal.created_at,
        RankedGameSignal.game_id,
        RankedGameSignal.human_principal_id,
    )
    rows = list((await session.execute(stmt)).scalars())
    scored = score_ranked_signal_rows(
        [_to_signal_row(row) for row in rows],
        thresholds=thresholds_from_settings(settings),
        now=_epoch_seconds(now),
    )
    inserted: list[RankedFlag] = []
    for flag in scored:
        row = await ranked_flags_repo.record_ranked_flag(
            session,
            flag=flag,
            created_at=now,
        )
        if row is not None:
            inserted.append(row)
    return inserted


def _to_signal_row(row: RankedGameSignal) -> RankedSignalRow:
    return RankedSignalRow(
        game_id=str(row.game_id),
        principal_id=str(row.human_principal_id),
        source_ip_hash=row.source_ip_hash,
        session_fingerprint_hash=row.session_fingerprint_hash,
        winner=row.winner,
        faction=row.faction,
        signals=_signals_dict(row.signals),
        observed_at=_epoch_seconds(row.created_at),
    )


def _signals_dict(value: Any) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _epoch_seconds(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(aware.timestamp())


__all__ = ["score_ranked_flags", "thresholds_from_settings"]
