"""Deterministic per-game analytics from the internal event log (US-102).

Pure functions — no clock reads, no DB, no I/O, no random.
Accepts the full raw internal event log (including SYSTEM events) so that
role assignments from ``RolesAssigned`` are available for per-role metrics.

Materialization contract
------------------------
``compute_game_analytics`` returns a ``GameAnalytics`` value object for one
game.  Callers that want to persist aggregates keyed by
``(ruleset_id, agent_build_id, version)`` across multiple games should
accumulate per-game ``GameAnalytics`` objects; the aggregation helpers and
the DB-backed ``AnalyticsRepository`` live in ``padrino.analytics.repository``
(added in US-104).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PHASE_DAY_RE: re.Pattern[str] = re.compile(r"(?:DAY|NIGHT)_(\d+)")


# ---------------------------------------------------------------------------
# Per-game result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleWinRate:
    """Win/loss counts for one role within a single game."""

    role: str
    wins: int
    games: int

    @property
    def rate(self) -> float:
        """Fraction of appearances won (0.0 when games == 0)."""
        return self.wins / self.games if self.games > 0 else 0.0


@dataclass(frozen=True)
class VotingAccuracy:
    """Fraction of non-abstain day votes that targeted an actual MAFIA player."""

    total_votes: int
    accurate_votes: int

    @property
    def rate(self) -> float:
        """Fraction of accurate votes (0.0 when total_votes == 0)."""
        return self.accurate_votes / self.total_votes if self.total_votes > 0 else 0.0


@dataclass(frozen=True)
class SurvivalPoint:
    """Alive-count snapshot for one role at the end of a given day number."""

    role: str
    day: int
    alive_count: int
    total_count: int

    @property
    def fraction(self) -> float:
        """Fraction alive (0.0 when total_count == 0)."""
        return self.alive_count / self.total_count if self.total_count > 0 else 0.0


@dataclass(frozen=True)
class GameAnalytics:
    """All deterministic analytics for a single completed game."""

    winner: str | None
    role_win_rates: tuple[RoleWinRate, ...]
    voting_accuracy: VotingAccuracy
    survival_curve: tuple[SurvivalPoint, ...]


# ---------------------------------------------------------------------------
# Relational analytics result types (US-103)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRecord:
    """One structured role claim emitted by a player during a game."""

    player_id: str
    claimed_role: str
    sequence: int
    phase: str


@dataclass(frozen=True)
class CounterClaimGroup:
    """Two or more players who claimed the same role in the same game."""

    claimed_role: str
    claimants: tuple[str, ...]  # sorted player_ids


@dataclass(frozen=True)
class ClaimAnalysis:
    """All role claims and counter-claims extracted from a single game's event log."""

    claims: tuple[ClaimRecord, ...]
    counter_claims: tuple[CounterClaimGroup, ...]


@dataclass(frozen=True)
class ParticipantStats:
    """Deterministic single-game stats for one participant (US-145).

    All metrics are derived purely from one game's internal event log plus the
    participant's seat (``public_player_id``).  Counts (not floats) are the
    storable primitives so they aggregate across games by summation; the rate
    ``@property`` helpers are conveniences and are never persisted as floats.

    ``faction`` / ``role`` are the participant's assignment in this game.
    ``won`` / ``drew`` / ``lost`` are one-hot for the single game.  Survival is
    1 when the participant was never eliminated before ``GameTerminated``.
    Voting accuracy counts the participant's own non-abstain day votes that hit
    an actual MAFIA seat; detection accuracy counts the participant's own
    detective investigations that returned a ``MAFIA`` finding.
    """

    public_player_id: str
    role: str
    faction: str
    won: int
    drew: int
    lost: int
    survived: int
    voting_total: int
    voting_accurate: int
    detection_total: int
    detection_accurate: int

    @property
    def voting_accuracy(self) -> float:
        """Fraction of the participant's non-abstain day votes that hit MAFIA."""
        return self.voting_accurate / self.voting_total if self.voting_total > 0 else 0.0

    @property
    def detection_accuracy(self) -> float:
        """Fraction of the participant's investigations that found MAFIA."""
        return self.detection_accurate / self.detection_total if self.detection_total > 0 else 0.0


@dataclass(frozen=True)
class HeadToHeadEntry:
    """Cross-faction head-to-head record between two agents across one or more games.

    ``agent_a`` is always lexicographically smaller than ``agent_b`` so the
    pair is canonical — no duplicate reversed entries exist in a matrix.
    ``a_wins`` counts games where ``agent_a``'s faction was the winner;
    ``b_wins`` counts games where ``agent_b``'s faction was the winner.
    """

    agent_a: str
    agent_b: str
    a_wins: int
    b_wins: int


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _extract_role_map(events: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """Return ``{player_id: (role, faction)}`` from the SYSTEM RolesAssigned event."""
    for event in events:
        if event.get("event_type") == "RolesAssigned":
            assignments = event.get("payload", {}).get("assignments", [])
            return {
                str(a["public_player_id"]): (str(a["role"]), str(a["faction"]))
                for a in assignments
                if "public_player_id" in a and "role" in a and "faction" in a
            }
    return {}


def _extract_winner(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("event_type") == "GameTerminated":
            winner = event.get("payload", {}).get("winner")
            return str(winner) if winner is not None else None
    return None


def _phase_to_day(phase: str) -> int:
    """Extract numeric day from a phase label such as ``DAY_2_VOTE`` or ``NIGHT_1_ACTIONS``."""
    match = _PHASE_DAY_RE.search(phase)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Analytics computation
# ---------------------------------------------------------------------------


def _compute_role_win_rates(
    winner: str | None,
    role_map: dict[str, tuple[str, str]],
) -> tuple[RoleWinRate, ...]:
    """Per-role win counts for one game.  DRAW is counted as a loss for all factions."""
    if not winner or not role_map:
        return ()
    stats: dict[str, list[int]] = {}  # role -> [wins, games]
    for _pid, (role, faction) in role_map.items():
        if role not in stats:
            stats[role] = [0, 0]
        stats[role][1] += 1
        if faction == winner:
            stats[role][0] += 1
    return tuple(RoleWinRate(role=r, wins=s[0], games=s[1]) for r, s in sorted(stats.items()))


def _compute_voting_accuracy(
    events: Sequence[Mapping[str, Any]],
    role_map: dict[str, tuple[str, str]],
) -> VotingAccuracy:
    """Fraction of non-abstain day votes that targeted an actual MAFIA player."""
    mafia_players = {pid for pid, (_, faction) in role_map.items() if faction == "MAFIA"}
    total = 0
    accurate = 0
    for event in events:
        if event.get("event_type") != "VoteSubmitted":
            continue
        payload = event.get("payload", {})
        if payload.get("is_abstain"):
            continue
        target = payload.get("target")
        if target is None:
            continue
        total += 1
        if target in mafia_players:
            accurate += 1
    return VotingAccuracy(total_votes=total, accurate_votes=accurate)


def _compute_survival_curve(
    events: Sequence[Mapping[str, Any]],
    role_map: dict[str, tuple[str, str]],
) -> tuple[SurvivalPoint, ...]:
    """Alive-count per role per day across the game timeline."""
    role_players: dict[str, list[str]] = {}
    for pid, (role, _) in role_map.items():
        role_players.setdefault(role, []).append(pid)

    # Track when each player was eliminated and the max day in the event log.
    eliminated_at: dict[str, int] = {}
    max_day = 0
    for event in events:
        day = _phase_to_day(str(event.get("phase", "")))
        if day > max_day:
            max_day = day
        if event.get("event_type") == "PlayerEliminated":
            pid = event.get("payload", {}).get("public_player_id")
            if pid is not None:
                eliminated_at[str(pid)] = day

    result: list[SurvivalPoint] = []
    for role in sorted(role_players):
        players = role_players[role]
        total = len(players)
        for day in range(0, max_day + 1):
            # A player is alive at end of day D if eliminated_at[p] > D.
            # eliminated_at[p] == D means eliminated during day D → not alive at end of D.
            alive = sum(1 for p in players if eliminated_at.get(p, max_day + 1) > day)
            result.append(SurvivalPoint(role=role, day=day, alive_count=alive, total_count=total))

    return tuple(result)


def compute_game_analytics(events: Sequence[Mapping[str, Any]]) -> GameAnalytics:
    """Compute all deterministic analytics for one game from its full internal event log.

    Parameters
    ----------
    events:
        The complete raw event log for one game, in sequence order.
        SYSTEM-visibility events (e.g. ``RolesAssigned``) must be included;
        this function reads role and faction data from them.

    Returns
    -------
    GameAnalytics
        Immutable value object with per-role win rates, voting accuracy, and
        survival-curve snapshots.  All three metrics are safe to store and
        aggregate across games — no model identity or private chat content
        is present.
    """
    role_map = _extract_role_map(events)
    winner = _extract_winner(events)

    return GameAnalytics(
        winner=winner,
        role_win_rates=_compute_role_win_rates(winner, role_map),
        voting_accuracy=_compute_voting_accuracy(events, role_map),
        survival_curve=_compute_survival_curve(events, role_map),
    )


def compute_claim_analysis(events: Sequence[Mapping[str, Any]]) -> ClaimAnalysis:
    """Extract role claims and detect counter-claims from the structured event log.

    Only ``RoleClaimed`` events (structured, PUBLIC) are read — free-text chat
    is never parsed (Hard rule 2).  A counter-claim is when two or more distinct
    players claim the same role within the same game.
    """
    claims: list[ClaimRecord] = []
    for event in events:
        if event.get("event_type") != "RoleClaimed":
            continue
        actor = event.get("actor_player_id")
        if actor is None:
            continue
        payload = event.get("payload", {})
        claimed_role = payload.get("claimed_role")
        if claimed_role is None:
            continue
        claims.append(
            ClaimRecord(
                player_id=str(actor),
                claimed_role=str(claimed_role),
                sequence=int(event.get("sequence", 0)),
                phase=str(event.get("phase", "")),
            )
        )

    by_role: dict[str, list[str]] = {}
    for claim in claims:
        by_role.setdefault(claim.claimed_role, []).append(claim.player_id)

    counter_claims = tuple(
        CounterClaimGroup(
            claimed_role=role,
            claimants=tuple(sorted(set(pids))),
        )
        for role, pids in sorted(by_role.items())
        if len(set(pids)) >= 2
    )

    return ClaimAnalysis(claims=tuple(claims), counter_claims=counter_claims)


def compute_head_to_head_matrix(
    events: Sequence[Mapping[str, Any]],
    agent_map: Mapping[str, str],
) -> tuple[HeadToHeadEntry, ...]:
    """Compute cross-faction head-to-head records between agents for one game.

    Parameters
    ----------
    events:
        Full internal event log for one game (must include ``RolesAssigned``
        and ``GameTerminated``).
    agent_map:
        ``{player_id: agent_build_id}`` mapping injected by the caller (not in
        the event log) so this pure function remains DB-free.

    Returns a tuple of :class:`HeadToHeadEntry` values, one per cross-faction
    pair of agents.  Entries are canonical: ``agent_a < agent_b``
    lexicographically, so no reversed duplicates exist.  Same-faction pairs are
    excluded (they cooperate, not compete).
    """
    role_map = _extract_role_map(events)
    winner = _extract_winner(events)

    if not role_map or not agent_map or winner is None:
        return ()

    agent_faction: dict[str, str] = {}
    for pid, (_role, faction) in role_map.items():
        agent_id = agent_map.get(str(pid))
        if agent_id is not None:
            agent_faction[agent_id] = faction

    agents = sorted(agent_faction.keys())
    entries: dict[tuple[str, str], list[int]] = {}

    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            id_i = agents[i]
            id_j = agents[j]
            if agent_faction[id_i] == agent_faction[id_j]:
                continue

            key_a, key_b = (id_i, id_j) if id_i < id_j else (id_j, id_i)
            if (key_a, key_b) not in entries:
                entries[(key_a, key_b)] = [0, 0]

            if winner != "DRAW":
                fac_a = agent_faction[key_a]
                if fac_a == winner:
                    entries[(key_a, key_b)][0] += 1
                else:
                    entries[(key_a, key_b)][1] += 1

    return tuple(
        HeadToHeadEntry(agent_a=a, agent_b=b, a_wins=w[0], b_wins=w[1])
        for (a, b), w in sorted(entries.items())
    )


def _extract_eliminated(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return the set of ``public_player_id`` eliminated during the game."""
    eliminated: set[str] = set()
    for event in events:
        if event.get("event_type") == "PlayerEliminated":
            pid = event.get("payload", {}).get("public_player_id")
            if pid is not None:
                eliminated.add(str(pid))
    return eliminated


def _per_seat_voting(
    events: Sequence[Mapping[str, Any]],
    role_map: dict[str, tuple[str, str]],
) -> dict[str, list[int]]:
    """Per-actor ``[total, accurate]`` for non-abstain day votes that hit MAFIA."""
    mafia_players = {pid for pid, (_, faction) in role_map.items() if faction == "MAFIA"}
    by_actor: dict[str, list[int]] = {}
    for event in events:
        if event.get("event_type") != "VoteSubmitted":
            continue
        actor = event.get("actor_player_id")
        if actor is None:
            continue
        payload = event.get("payload", {})
        if payload.get("is_abstain"):
            continue
        target = payload.get("target")
        if target is None:
            continue
        acc = by_actor.setdefault(str(actor), [0, 0])
        acc[0] += 1
        if target in mafia_players:
            acc[1] += 1
    return by_actor


def _per_seat_detection(events: Sequence[Mapping[str, Any]]) -> dict[str, list[int]]:
    """Per-detective ``[total, accurate]`` from ``DetectiveResultDelivered`` events.

    The detective's investigation finding (``MAFIA`` / ``TOWN``) is mechanical
    truth; an accurate detection is one that surfaced an actual MAFIA seat.  Only
    the structured event is read — free-text chat is never parsed (Hard rule 2).
    """
    by_actor: dict[str, list[int]] = {}
    for event in events:
        if event.get("event_type") != "DetectiveResultDelivered":
            continue
        actor = event.get("actor_player_id")
        if actor is None:
            continue
        finding = event.get("payload", {}).get("finding")
        if finding is None:
            continue
        acc = by_actor.setdefault(str(actor), [0, 0])
        acc[0] += 1
        if finding == "MAFIA":
            acc[1] += 1
    return by_actor


def compute_participant_stats(
    events: Sequence[Mapping[str, Any]],
    participant_map: Mapping[str, object],
) -> tuple[ParticipantStats, ...]:
    """Compute per-participant single-game stats for the seats in ``participant_map``.

    Parameters
    ----------
    events:
        The complete raw internal event log for one game (must include the
        SYSTEM ``RolesAssigned`` and the terminal ``GameTerminated``).
    participant_map:
        ``{public_player_id: principal_id}`` injected by the caller — the seat
        identities to compute stats for (e.g. the human-occupied seats of a
        human-lane game).  Keeps this function DB-free and pure; the
        ``principal_id`` values are not read here (the caller keys persistence by
        them), only the ``public_player_id`` keys select which seats to score.

    Returns one :class:`ParticipantStats` per seat present in BOTH
    ``participant_map`` and the game's role assignments, ordered by
    ``public_player_id``.  Seats absent from the role map (unknown to the game)
    are skipped.  A DRAW is neither a win nor a loss for any faction.
    """
    role_map = _extract_role_map(events)
    winner = _extract_winner(events)
    eliminated = _extract_eliminated(events)
    voting = _per_seat_voting(events, role_map)
    detection = _per_seat_detection(events)

    results: list[ParticipantStats] = []
    for pid in sorted(participant_map):
        assignment = role_map.get(pid)
        if assignment is None:
            continue
        role, faction = assignment
        won = 1 if winner is not None and winner != "DRAW" and faction == winner else 0
        drew = 1 if winner == "DRAW" else 0
        lost = 1 if winner is not None and not won and not drew else 0
        survived = 0 if pid in eliminated else 1
        vote_acc = voting.get(pid, [0, 0])
        det_acc = detection.get(pid, [0, 0])
        results.append(
            ParticipantStats(
                public_player_id=pid,
                role=role,
                faction=faction,
                won=won,
                drew=drew,
                lost=lost,
                survived=survived,
                voting_total=vote_acc[0],
                voting_accurate=vote_acc[1],
                detection_total=det_acc[0],
                detection_accurate=det_acc[1],
            )
        )
    return tuple(results)


__all__ = [
    "ClaimAnalysis",
    "ClaimRecord",
    "CounterClaimGroup",
    "GameAnalytics",
    "HeadToHeadEntry",
    "ParticipantStats",
    "RoleWinRate",
    "SurvivalPoint",
    "VotingAccuracy",
    "compute_claim_analysis",
    "compute_game_analytics",
    "compute_head_to_head_matrix",
    "compute_participant_stats",
]
