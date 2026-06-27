"""Deterministic advisory scoring over ranked human telemetry rows."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from math import comb
from typing import Literal, TypeAlias

Severity: TypeAlias = Literal["low", "medium", "high"]
FlagKind: TypeAlias = Literal[
    "same_ip_cluster",
    "repeated_cooccurrence",
    "abnormal_win_streak",
    "abnormal_loss_streak",
    "bot_cadence",
]


@dataclass(frozen=True, slots=True)
class RankedSignalRow:
    """Pure projection of one ranked-game telemetry row."""

    game_id: str
    principal_id: str
    source_ip_hash: str
    session_fingerprint_hash: str | None
    winner: str
    faction: str
    signals: Mapping[str, object]
    observed_at: int


@dataclass(frozen=True, slots=True)
class RankedSignalThresholds:
    """Operator-tunable thresholds for advisory ranked flag scoring."""

    window_seconds: int = 30 * 24 * 60 * 60
    same_ip_min_principals: int = 3
    cooccurrence_min_games: int = 10
    streak_min_games: int = 8
    streak_max_tail_probability: float = 0.01
    bot_cadence_min_games: int = 5
    bot_cadence_max_average_latency_ms: int = 750
    bot_cadence_max_latency_spread_ms: int = 25
    bot_cadence_latency_keys: tuple[str, ...] = field(
        default=("action_latency_ms", "response_latency_ms", "turn_latency_ms")
    )


@dataclass(frozen=True, slots=True)
class RankedFlag:
    """One deterministic advisory flag for manual ranked review."""

    kind: FlagKind
    principal_id: str
    severity: Severity
    evidence: dict[str, object]


def score_ranked_signals(
    rows: Iterable[RankedSignalRow],
    *,
    thresholds: RankedSignalThresholds,
    now: int,
) -> list[RankedFlag]:
    """Return deterministic advisory flags without mutating ratings or games."""
    window_rows = _windowed_rows(rows, thresholds=thresholds, now=now)
    flags = [
        *_same_ip_cluster_flags(window_rows, thresholds),
        *_repeated_cooccurrence_flags(window_rows, thresholds),
        *_streak_flags(window_rows, thresholds),
        *_bot_cadence_flags(window_rows, thresholds),
    ]
    return sorted(flags, key=_flag_sort_key)


def _windowed_rows(
    rows: Iterable[RankedSignalRow],
    *,
    thresholds: RankedSignalThresholds,
    now: int,
) -> list[RankedSignalRow]:
    return sorted(
        (
            row
            for row in rows
            if 0 <= now - row.observed_at <= thresholds.window_seconds
            and row.principal_id
            and row.game_id
        ),
        key=_row_sort_key,
    )


def _same_ip_cluster_flags(
    rows: Sequence[RankedSignalRow],
    thresholds: RankedSignalThresholds,
) -> list[RankedFlag]:
    by_ip: dict[str, list[RankedSignalRow]] = defaultdict(list)
    for row in rows:
        if row.source_ip_hash:
            by_ip[row.source_ip_hash].append(row)

    flags: list[RankedFlag] = []
    for source_ip_hash, group in sorted(by_ip.items()):
        principal_ids = sorted({row.principal_id for row in group})
        if len(principal_ids) < thresholds.same_ip_min_principals:
            continue
        evidence: dict[str, object] = {
            "distinct_principal_count": len(principal_ids),
            "game_count": len({row.game_id for row in group}),
            "game_ids": sorted({row.game_id for row in group}),
            "principal_ids": principal_ids,
            "source_ip_hash": source_ip_hash,
            "window_seconds": thresholds.window_seconds,
        }
        for principal_id in principal_ids:
            flags.append(
                RankedFlag(
                    kind="same_ip_cluster",
                    principal_id=principal_id,
                    severity="medium",
                    evidence=evidence,
                )
            )
    return flags


def _repeated_cooccurrence_flags(
    rows: Sequence[RankedSignalRow],
    thresholds: RankedSignalThresholds,
) -> list[RankedFlag]:
    by_cluster: dict[str, list[RankedSignalRow]] = defaultdict(list)
    for row in rows:
        if row.source_ip_hash:
            by_cluster[f"source_ip:{row.source_ip_hash}"].append(row)
        if row.session_fingerprint_hash:
            by_cluster[f"device:{row.session_fingerprint_hash}"].append(row)

    flags: list[RankedFlag] = []
    for cluster_key, group in sorted(by_cluster.items()):
        principals = sorted({row.principal_id for row in group})
        if len(principals) < 2:
            continue
        for left, right in combinations(principals, 2):
            pair_rows = [row for row in group if row.principal_id in {left, right}]
            pair_game_ids = sorted({row.game_id for row in pair_rows})
            if len(pair_game_ids) < thresholds.cooccurrence_min_games:
                continue
            for principal_id, peer_id in ((left, right), (right, left)):
                flags.append(
                    RankedFlag(
                        kind="repeated_cooccurrence",
                        principal_id=principal_id,
                        severity="medium",
                        evidence={
                            "cluster_key": cluster_key,
                            "game_count": len(pair_game_ids),
                            "game_ids": pair_game_ids,
                            "peer_principal_id": peer_id,
                            "principal_ids": [left, right],
                            "window_seconds": thresholds.window_seconds,
                        },
                    )
                )
    return flags


def _streak_flags(
    rows: Sequence[RankedSignalRow],
    thresholds: RankedSignalThresholds,
) -> list[RankedFlag]:
    by_principal: dict[str, list[RankedSignalRow]] = defaultdict(list)
    for row in rows:
        by_principal[row.principal_id].append(row)

    flags: list[RankedFlag] = []
    for principal_id, group in sorted(by_principal.items()):
        ordered = sorted(group, key=_row_sort_key)
        outcomes = [_outcome(row) for row in ordered]
        wins = outcomes.count("win")
        losses = outcomes.count("loss")
        total = wins + losses
        if total < thresholds.streak_min_games:
            continue

        max_win_streak = _max_run(outcomes, "win")
        win_tail = _binomial_upper_tail(wins, total)
        if (
            max_win_streak >= thresholds.streak_min_games
            and win_tail <= thresholds.streak_max_tail_probability
        ):
            flags.append(
                RankedFlag(
                    kind="abnormal_win_streak",
                    principal_id=principal_id,
                    severity="high",
                    evidence={
                        "game_count": total,
                        "losses": losses,
                        "max_win_streak": max_win_streak,
                        "tail_probability": _probability_string(win_tail),
                        "wins": wins,
                        "window_seconds": thresholds.window_seconds,
                    },
                )
            )

        max_loss_streak = _max_run(outcomes, "loss")
        loss_tail = _binomial_upper_tail(losses, total)
        if (
            max_loss_streak >= thresholds.streak_min_games
            and loss_tail <= thresholds.streak_max_tail_probability
        ):
            flags.append(
                RankedFlag(
                    kind="abnormal_loss_streak",
                    principal_id=principal_id,
                    severity="medium",
                    evidence={
                        "game_count": total,
                        "losses": losses,
                        "max_loss_streak": max_loss_streak,
                        "tail_probability": _probability_string(loss_tail),
                        "wins": wins,
                        "window_seconds": thresholds.window_seconds,
                    },
                )
            )
    return flags


def _bot_cadence_flags(
    rows: Sequence[RankedSignalRow],
    thresholds: RankedSignalThresholds,
) -> list[RankedFlag]:
    by_principal: dict[str, list[RankedSignalRow]] = defaultdict(list)
    for row in rows:
        by_principal[row.principal_id].append(row)

    flags: list[RankedFlag] = []
    for principal_id, group in sorted(by_principal.items()):
        for latency_key in thresholds.bot_cadence_latency_keys:
            latencies = [
                latency
                for row in group
                if (latency := _positive_int(row.signals.get(latency_key))) is not None
            ]
            if len(latencies) < thresholds.bot_cadence_min_games:
                continue
            average = sum(latencies) / len(latencies)
            spread = max(latencies) - min(latencies)
            if (
                average <= thresholds.bot_cadence_max_average_latency_ms
                and spread <= thresholds.bot_cadence_max_latency_spread_ms
            ):
                flags.append(
                    RankedFlag(
                        kind="bot_cadence",
                        principal_id=principal_id,
                        severity="medium",
                        evidence={
                            "average_latency_ms": _stable_number(average),
                            "game_count": len(group),
                            "latency_key": latency_key,
                            "latency_spread_ms": spread,
                            "sample_count": len(latencies),
                            "window_seconds": thresholds.window_seconds,
                        },
                    )
                )
                break
    return flags


def _row_sort_key(row: RankedSignalRow) -> tuple[int, str, str]:
    return (row.observed_at, row.game_id, row.principal_id)


def _flag_sort_key(flag: RankedFlag) -> tuple[str, str, str]:
    return (
        flag.kind,
        flag.principal_id,
        json.dumps(flag.evidence, sort_keys=True, separators=(",", ":")),
    )


def _outcome(row: RankedSignalRow) -> Literal["win", "loss", "draw"]:
    if row.winner == "DRAW":
        return "draw"
    return "win" if row.winner == row.faction else "loss"


def _max_run(outcomes: Sequence[str], target: str) -> int:
    longest = 0
    current = 0
    for outcome in outcomes:
        if outcome == target:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _binomial_upper_tail(successes: int, total: int) -> float:
    if total <= 0:
        return 1.0
    numerator = sum(comb(total, k) for k in range(successes, total + 1))
    return float(numerator / (2**total))


def _probability_string(value: float) -> str:
    return format(value, ".12g")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _stable_number(value: float) -> int | str:
    if value.is_integer():
        return int(value)
    return format(value, ".12g")


__all__ = [
    "RankedFlag",
    "RankedSignalRow",
    "RankedSignalThresholds",
    "score_ranked_signals",
]
