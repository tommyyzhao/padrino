"""Materialized per-game recap analytics store (US-120).

A finished game's deterministic per-game analytics + claim analysis are
identical for the lifetime of the game (the event log is immutable once the
game terminates), so re-deriving them from the full event log on every recap
request is wasted work.  This module computes them once when a game becomes
RECENT and persists the full, outcome-revealing payload keyed by ``game_id``.

The serialization shape produced by :func:`build_game_analytics_payload` is the
full (RECENT) form of the ``/public/games/{id}/analytics`` response: it includes
``winner`` and ``role_win_rates``.  The LIVE spoiler-safe path nulls those at
serve time, so the stored blob is only ever read for RECENT games whose outcome
is already public.

``build_game_analytics_payload`` is pure (no I/O); ``materialize_game_analytics``
and ``get_materialized_analytics`` are the impure DB seam.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.analytics.deterministic import compute_claim_analysis, compute_game_analytics
from padrino.db.models import GameEvent, MaterializedGameAnalytics


def build_game_analytics_payload(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute the full per-game analytics + claim analysis as a JSON-able dict.

    Pure: the same event log always yields the same payload.  The shape mirrors
    the RECENT (outcome-revealing) form of ``PublicGameAnalyticsResponse``;
    ``game_id`` and ``ruleset_id`` are NOT included here because they are stamped
    by the caller from the game row.
    """
    analytics = compute_game_analytics(events)
    claim_analysis = compute_claim_analysis(events)
    return {
        "winner": analytics.winner,
        "voting_accuracy": {
            "total_votes": analytics.voting_accuracy.total_votes,
            "accurate_votes": analytics.voting_accuracy.accurate_votes,
            "rate": analytics.voting_accuracy.rate,
        },
        "survival_curve": [
            {
                "role": sp.role,
                "day": sp.day,
                "alive_count": sp.alive_count,
                "total_count": sp.total_count,
                "fraction": sp.fraction,
            }
            for sp in analytics.survival_curve
        ],
        "role_win_rates": [
            {
                "role": rwr.role,
                "wins": rwr.wins,
                "games": rwr.games,
                "rate": rwr.rate,
            }
            for rwr in analytics.role_win_rates
        ],
        "claims": [
            {
                "player_id": cr.player_id,
                "claimed_role": cr.claimed_role,
                "sequence": cr.sequence,
                "phase": cr.phase,
            }
            for cr in claim_analysis.claims
        ],
        "counter_claims": [
            {
                "claimed_role": ccg.claimed_role,
                "claimants": list(ccg.claimants),
            }
            for ccg in claim_analysis.counter_claims
        ],
    }


async def _load_event_dicts(session: AsyncSession, game_id: uuid.UUID) -> list[dict[str, Any]]:
    """Load a game's event log ordered by sequence as plain dicts."""
    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    raw_events = list((await session.execute(stmt)).scalars())
    return [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in raw_events
    ]


async def get_materialized_analytics(
    session: AsyncSession, game_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the stored analytics payload for a game, or ``None`` if not materialized."""
    row = await session.get(MaterializedGameAnalytics, game_id)
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row.analytics_json)
    return payload


async def materialize_game_analytics(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    ruleset_id: str,
) -> dict[str, Any]:
    """Compute the full per-game analytics from the event log and persist them.

    Idempotent: if a row already exists it is overwritten with a fresh compute
    (the inputs are immutable, so the result is identical).  Returns the payload
    so callers (the on-the-fly backfill path) can serve it without a re-read.
    """
    payload = build_game_analytics_payload(await _load_event_dicts(session, game_id))
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    existing = await session.get(MaterializedGameAnalytics, game_id)
    if existing is None:
        session.add(
            MaterializedGameAnalytics(
                game_id=game_id,
                ruleset_id=ruleset_id,
                analytics_json=encoded,
            )
        )
    else:
        existing.ruleset_id = ruleset_id
        existing.analytics_json = encoded
    await session.flush()
    return payload


__all__ = [
    "build_game_analytics_payload",
    "get_materialized_analytics",
    "materialize_game_analytics",
]
