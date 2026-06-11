"""DB-backed materialization of deterministic analytics aggregates (US-104).

``AnalyticsAggregate`` rows back ``GET /public/models/{id}/analytics``. This
module is their producer: :func:`refresh_analytics_aggregates_for_game` is
called after a game completes (continuous matchmaking tick) and recomputes the
aggregate for every agent build seated in that game.

Semantics match the ``AnalyticsAggregate`` model docstring: the aggregate for
an agent build is :func:`padrino.analytics.deterministic.compute_game_analytics`
rolled up across every COMPLETED game of the ruleset the agent was seated in.
Recompute-from-scratch keeps the operation idempotent under the
``(ruleset_id, agent_build_id, version)`` unique constraint.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.analytics.deterministic import compute_game_analytics
from padrino.db.models import AgentBuild, AnalyticsAggregate, Game, GameEvent, GameSeat

GAME_STATUS_COMPLETED = "COMPLETED"


async def _load_event_dicts(session: AsyncSession, game_id: uuid.UUID) -> list[dict[str, Any]]:
    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    return [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
        }
        for e in (await session.execute(stmt)).scalars()
    ]


async def refresh_analytics_aggregate(
    session: AsyncSession,
    *,
    agent_build_id: uuid.UUID,
    ruleset_id: str,
    now: datetime | None = None,
) -> AnalyticsAggregate | None:
    """Recompute and upsert the aggregate row for one (agent build, ruleset).

    Returns ``None`` when the build is unknown or has no completed games yet.
    """
    build = await session.get(AgentBuild, agent_build_id)
    if build is None:
        return None

    games_stmt = (
        select(Game.id)
        .join(GameSeat, GameSeat.game_id == Game.id)
        .where(
            GameSeat.agent_build_id == agent_build_id,
            Game.ruleset_id == ruleset_id,
            Game.status == GAME_STATUS_COMPLETED,
        )
        .distinct()
    )
    game_ids = list((await session.execute(games_stmt)).scalars())
    if not game_ids:
        return None

    role_rates: dict[str, list[int]] = {}  # role -> [wins, games]
    total_votes = 0
    accurate_votes = 0
    survival: dict[tuple[str, int], list[int]] = {}  # (role, day) -> [alive, total]

    for gid in game_ids:
        analytics = compute_game_analytics(await _load_event_dicts(session, gid))
        for rate in analytics.role_win_rates:
            role_acc = role_rates.setdefault(rate.role, [0, 0])
            role_acc[0] += rate.wins
            role_acc[1] += rate.games
        total_votes += analytics.voting_accuracy.total_votes
        accurate_votes += analytics.voting_accuracy.accurate_votes
        for point in analytics.survival_curve:
            surv_acc = survival.setdefault((point.role, point.day), [0, 0])
            surv_acc[0] += point.alive_count
            surv_acc[1] += point.total_count

    role_win_rates_json = json.dumps(
        [
            {"role": role, "wins": wins, "games": games}
            for role, (wins, games) in sorted(role_rates.items())
        ],
        separators=(",", ":"),
    )
    survival_curve_json = json.dumps(
        [
            {"role": role, "day": day, "alive_count": alive, "total_count": total}
            for (role, day), (alive, total) in sorted(survival.items())
        ],
        separators=(",", ":"),
    )

    existing_stmt = select(AnalyticsAggregate).where(
        AnalyticsAggregate.ruleset_id == ruleset_id,
        AnalyticsAggregate.agent_build_id == agent_build_id,
        AnalyticsAggregate.version == build.version,
    )
    row = (await session.execute(existing_stmt)).scalar_one_or_none()
    computed_at = now if now is not None else datetime.now(UTC)
    if row is None:
        row = AnalyticsAggregate(
            ruleset_id=ruleset_id,
            agent_build_id=agent_build_id,
            version=build.version,
            games_played=len(game_ids),
            role_win_rates_json=role_win_rates_json,
            voting_total_votes=total_votes,
            voting_accurate_votes=accurate_votes,
            survival_curve_json=survival_curve_json,
            computed_at=computed_at,
        )
        session.add(row)
    else:
        row.games_played = len(game_ids)
        row.role_win_rates_json = role_win_rates_json
        row.voting_total_votes = total_votes
        row.voting_accurate_votes = accurate_votes
        row.survival_curve_json = survival_curve_json
        row.computed_at = computed_at
    await session.flush()
    return row


async def refresh_analytics_aggregates_for_game(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> list[AnalyticsAggregate]:
    """Refresh the aggregate of every agent build seated in *game_id*.

    No-op (returns ``[]``) unless the game exists and is COMPLETED.
    """
    game = await session.get(Game, game_id)
    if game is None or game.status != GAME_STATUS_COMPLETED:
        return []

    seats_stmt = select(GameSeat.agent_build_id).where(GameSeat.game_id == game_id).distinct()
    refreshed: list[AnalyticsAggregate] = []
    for agent_build_id in (await session.execute(seats_stmt)).scalars():
        if agent_build_id is None:
            continue
        row = await refresh_analytics_aggregate(
            session,
            agent_build_id=agent_build_id,
            ruleset_id=game.ruleset_id,
            now=now,
        )
        if row is not None:
            refreshed.append(row)
    return refreshed


__all__ = [
    "refresh_analytics_aggregate",
    "refresh_analytics_aggregates_for_game",
]
