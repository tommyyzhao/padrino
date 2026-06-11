"""Curated-roster matchmaker: deterministic next-match selection (US-097).

Pure, deterministic: given a curated roster of approved AgentBuild UUIDs and
the match history, ``next_match`` selects the N agents with the fewest games
played (tie-broken by SeededRng) and assigns them to seats via a gauntlet seed
derived from ``seed + len(history)`` — the same faction-permutation semantics
used by the heterogeneous tournament runner.

No wall-clock, no ``random`` module, no DB access — callers inject the roster
and history. The roster is a curated list of approved AgentBuild IDs (no open
submission in this wave).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from padrino.core.engine.rng import SeededRng
from padrino.core.rulesets import get_ruleset
from padrino.core.seating import seat_permutation


@dataclass(frozen=True)
class MatchRecord:
    """Minimal record of a past match used for load-balancing the roster."""

    participants: tuple[uuid.UUID, ...]


@dataclass
class MatchPlan:
    """Deterministic plan for a single gauntlet run."""

    ruleset_id: str
    gauntlet_seed: str
    # Seat id (e.g. "P01") -> AgentBuild UUID; compatible with
    # ``run_tournament_from_roster`` in padrino.gauntlets.tournament.
    roster_by_seat: dict[str, uuid.UUID]


def _derive_gauntlet_seed(seed: str, history_len: int) -> str:
    """Deterministic gauntlet seed from external seed + history length."""
    return hashlib.sha256(
        b"matchmaker:" + seed.encode("utf-8") + b":" + str(history_len).encode()
    ).hexdigest()


def next_match(
    roster: list[uuid.UUID],
    history: list[MatchRecord],
    *,
    ruleset_id: str = "mini7_v1",
    seed: str,
) -> MatchPlan:
    """Return a deterministic :class:`MatchPlan` for the next game.

    Selects ``player_count`` agents from *roster*, preferring those with fewer
    games recorded in *history* (tie-broken deterministically by SeededRng).
    Assigns agents to seats via :func:`~padrino.core.seating.seat_permutation`
    with the derived gauntlet seed, reusing the faction-permutation semantics of
    the heterogeneous tournament runner so each agent rotates through roles
    across successive games.

    Raises :exc:`ValueError` when *roster* has fewer agents than the ruleset
    player count.
    """
    ruleset = get_ruleset(ruleset_id)
    player_count = ruleset.PLAYER_COUNT

    if len(roster) < player_count:
        raise ValueError(
            f"roster has {len(roster)} agents but ruleset {ruleset_id!r} "
            f"requires at least {player_count}"
        )

    gauntlet_seed = _derive_gauntlet_seed(seed, len(history))
    rng = SeededRng(gauntlet_seed)

    # Count games played per roster agent across the history.
    play_counts: dict[uuid.UUID, int] = dict.fromkeys(roster, 0)
    for record in history:
        for participant in record.participants:
            if participant in play_counts:
                play_counts[participant] += 1

    # Shuffle the roster for deterministic tie-breaking, then stable-sort
    # ascending by play count so least-played agents are preferred.
    shuffled = rng.shuffle(list(roster))
    selected: list[uuid.UUID] = sorted(shuffled, key=lambda a: play_counts[a])[:player_count]

    # Apply seat_permutation for gauntlet-compatible faction rotation:
    # seat i receives selected[perm[i]], matching tournament.run_heterogeneous_tournament.
    seat_ids = [f"P{i + 1:02d}" for i in range(player_count)]
    perm = seat_permutation(gauntlet_seed, player_count)
    roster_by_seat: dict[str, uuid.UUID] = {
        seat_ids[i]: selected[perm[i]] for i in range(player_count)
    }

    return MatchPlan(
        ruleset_id=ruleset_id,
        gauntlet_seed=gauntlet_seed,
        roster_by_seat=roster_by_seat,
    )


__all__ = ["MatchPlan", "MatchRecord", "next_match"]
