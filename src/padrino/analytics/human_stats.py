"""DB-backed materialization of per-human play-history stats (US-145 producer).

``HumanPlayerStats`` rows back a signed-in player's deterministic play history
(win rate by role/faction, survival, voting accuracy, detection accuracy), keyed
by ``(ruleset_id, principal_id)``.  This module is their producer:
:func:`refresh_human_player_stats_for_game` is called after a human-lane game
completes and recomputes the aggregate for every human principal seated in it.

Segregation (hard rule 8): stats are materialized ONLY from human-lane games — a
game with at least one seat a human occupied (``seat_kind`` in
``{HUMAN, AI_TAKEOVER}``).  A scientific-league (AI-only) game contributes ZERO
rows.  There is NO leaderboard or ELO in v1; the dormant ``human_rating`` table
stays empty.

Per-game stats come from the pure
:func:`padrino.analytics.deterministic.compute_participant_stats`; this module is
the thin impure shell that resolves the ``{public_player_id -> principal_id}``
seat map from the DB and persists the rolled-up counts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.analytics.deterministic import compute_participant_stats
from padrino.core.enums import SeatKind
from padrino.db.models import Game, GameEvent, GameSeat, HumanPlayerStats

GAME_STATUS_COMPLETED = "COMPLETED"

# A seat is "human-lane" when a human ever occupied it: a live human seat, or a
# seat an AI silently took over (matching ``runner.human_lane``).
_HUMAN_LANE_SEAT_KINDS = frozenset({SeatKind.HUMAN.value, SeatKind.AI_TAKEOVER.value})


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


async def _human_seat_map(session: AsyncSession, game_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Return ``{public_player_id: principal_id}`` for human-occupied seats only.

    A scientific (AI-only) game has no such seat, so the map is empty and the
    game contributes nothing (segregation).
    """
    stmt = select(GameSeat).where(GameSeat.game_id == game_id)
    out: dict[str, uuid.UUID] = {}
    for seat in (await session.execute(stmt)).scalars():
        if seat.seat_kind in _HUMAN_LANE_SEAT_KINDS and seat.occupant_principal_id is not None:
            out[seat.public_player_id] = seat.occupant_principal_id
    return out


def _win_rates_json(rates: dict[str, list[int]]) -> str:
    return json.dumps(
        [
            {"name": name, "wins": wins, "games": games}
            for name, (wins, games) in sorted(rates.items())
        ],
        separators=(",", ":"),
    )


async def refresh_human_player_stats(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    ruleset_id: str,
    now: datetime | None = None,
) -> HumanPlayerStats | None:
    """Recompute and upsert the stats row for one (principal, ruleset).

    Rolls up :func:`compute_participant_stats` across every COMPLETED human-lane
    game the principal occupied a seat in for the ruleset.  Returns ``None`` when
    the principal has no such completed games yet (so a scientific-only principal
    never gets a row).
    """
    seat_games = (
        select(GameSeat.game_id)
        .join(Game, Game.id == GameSeat.game_id)
        .where(
            GameSeat.occupant_principal_id == principal_id,
            GameSeat.seat_kind.in_(_HUMAN_LANE_SEAT_KINDS),
            Game.ruleset_id == ruleset_id,
            Game.status == GAME_STATUS_COMPLETED,
        )
        .distinct()
    )
    game_ids = list((await session.execute(seat_games)).scalars())
    if not game_ids:
        return None

    games = 0
    wins = 0
    draws = 0
    losses = 0
    survived = 0
    voting_total = 0
    voting_accurate = 0
    detection_total = 0
    detection_accurate = 0
    role_rates: dict[str, list[int]] = {}  # role -> [wins, games]
    faction_rates: dict[str, list[int]] = {}  # faction -> [wins, games]

    for gid in sorted(game_ids, key=str):
        seat_map = await _human_seat_map(session, gid)
        # Only this principal's seat(s) in the game.
        mine = {pid: pr for pid, pr in seat_map.items() if pr == principal_id}
        events = await _load_event_dicts(session, gid)
        for stat in compute_participant_stats(events, mine):
            games += 1
            wins += stat.won
            draws += stat.drew
            losses += stat.lost
            survived += stat.survived
            voting_total += stat.voting_total
            voting_accurate += stat.voting_accurate
            detection_total += stat.detection_total
            detection_accurate += stat.detection_accurate
            role_acc = role_rates.setdefault(stat.role, [0, 0])
            role_acc[0] += stat.won
            role_acc[1] += 1
            faction_acc = faction_rates.setdefault(stat.faction, [0, 0])
            faction_acc[0] += stat.won
            faction_acc[1] += 1

    if games == 0:
        return None

    existing_stmt = select(HumanPlayerStats).where(
        HumanPlayerStats.ruleset_id == ruleset_id,
        HumanPlayerStats.principal_id == principal_id,
    )
    row = (await session.execute(existing_stmt)).scalar_one_or_none()
    computed_at = now if now is not None else datetime.now(UTC)
    role_json = _win_rates_json(role_rates)
    faction_json = _win_rates_json(faction_rates)
    if row is None:
        row = HumanPlayerStats(
            ruleset_id=ruleset_id,
            principal_id=principal_id,
            games=games,
            wins=wins,
            draws=draws,
            losses=losses,
            role_win_rates_json=role_json,
            faction_win_rates_json=faction_json,
            survived_games=survived,
            voting_total_votes=voting_total,
            voting_accurate_votes=voting_accurate,
            detection_total=detection_total,
            detection_accurate=detection_accurate,
            computed_at=computed_at,
        )
        session.add(row)
    else:
        row.games = games
        row.wins = wins
        row.draws = draws
        row.losses = losses
        row.role_win_rates_json = role_json
        row.faction_win_rates_json = faction_json
        row.survived_games = survived
        row.voting_total_votes = voting_total
        row.voting_accurate_votes = voting_accurate
        row.detection_total = detection_total
        row.detection_accurate = detection_accurate
        row.computed_at = computed_at
    await session.flush()
    return row


async def refresh_human_player_stats_for_game(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> list[HumanPlayerStats]:
    """Refresh the stats of every human principal seated in *game_id*.

    No-op (returns ``[]``) unless the game exists, is COMPLETED, and is a
    human-lane game (at least one human-occupied seat).  A scientific (AI-only)
    game writes ZERO rows (segregation, hard rule 8).
    """
    game = await session.get(Game, game_id)
    if game is None or game.status != GAME_STATUS_COMPLETED:
        return []

    seat_map = await _human_seat_map(session, game_id)
    if not seat_map:
        return []

    refreshed: list[HumanPlayerStats] = []
    for principal_id in sorted(set(seat_map.values()), key=str):
        row = await refresh_human_player_stats(
            session,
            principal_id=principal_id,
            ruleset_id=game.ruleset_id,
            now=now,
        )
        if row is not None:
            refreshed.append(row)
    return refreshed


__all__ = [
    "refresh_human_player_stats",
    "refresh_human_player_stats_for_game",
]
