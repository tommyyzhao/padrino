"""US-312: pure ranked telemetry signal scoring."""

from __future__ import annotations

import ast
from pathlib import Path

from padrino.core.anticheat.signals import (
    RankedSignalRow,
    RankedSignalThresholds,
    score_ranked_signals,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "padrino" / "core" / "anticheat" / "signals.py"
)


def _row(
    game_id: str,
    principal_id: str,
    *,
    source_ip_hash: str = "ip-a",
    session_fingerprint_hash: str | None = None,
    winner: str = "TOWN",
    faction: str = "TOWN",
    observed_at: int = 1_000,
    signals: dict[str, object] | None = None,
) -> RankedSignalRow:
    return RankedSignalRow(
        game_id=game_id,
        principal_id=principal_id,
        source_ip_hash=source_ip_hash,
        session_fingerprint_hash=session_fingerprint_hash,
        winner=winner,
        faction=faction,
        signals=signals or {},
        observed_at=observed_at,
    )


def test_same_ip_cluster_flags_at_threshold_and_is_silent_below() -> None:
    thresholds = RankedSignalThresholds(
        window_seconds=600,
        same_ip_min_principals=3,
        cooccurrence_min_games=99,
        streak_min_games=99,
        bot_cadence_min_games=99,
    )
    below = [
        _row("g1", "p1", observed_at=950),
        _row("g2", "p2", observed_at=940),
    ]

    assert score_ranked_signals(below, thresholds=thresholds, now=1_000) == []

    flags = score_ranked_signals(
        [*below, _row("g3", "p3", observed_at=930)],
        thresholds=thresholds,
        now=1_000,
    )

    assert [(flag.kind, flag.principal_id, flag.severity) for flag in flags] == [
        ("same_ip_cluster", "p1", "medium"),
        ("same_ip_cluster", "p2", "medium"),
        ("same_ip_cluster", "p3", "medium"),
    ]
    assert flags[0].evidence == {
        "distinct_principal_count": 3,
        "game_count": 3,
        "game_ids": ["g1", "g2", "g3"],
        "principal_ids": ["p1", "p2", "p3"],
        "source_ip_hash": "ip-a",
        "window_seconds": 600,
    }


def test_repeated_cross_game_cooccurrence_flags_device_pair_only_at_threshold() -> None:
    thresholds = RankedSignalThresholds(
        window_seconds=600,
        same_ip_min_principals=99,
        cooccurrence_min_games=4,
        streak_min_games=99,
        bot_cadence_min_games=99,
    )
    below = [
        _row("g1", "p1", source_ip_hash="ip-1", session_fingerprint_hash="device-a"),
        _row("g2", "p2", source_ip_hash="ip-2", session_fingerprint_hash="device-a"),
        _row("g3", "p1", source_ip_hash="ip-3", session_fingerprint_hash="device-a"),
    ]

    assert score_ranked_signals(below, thresholds=thresholds, now=1_000) == []

    flags = score_ranked_signals(
        [*below, _row("g4", "p2", source_ip_hash="ip-4", session_fingerprint_hash="device-a")],
        thresholds=thresholds,
        now=1_000,
    )

    assert [(flag.kind, flag.principal_id, flag.severity) for flag in flags] == [
        ("repeated_cooccurrence", "p1", "medium"),
        ("repeated_cooccurrence", "p2", "medium"),
    ]
    assert flags[0].evidence == {
        "cluster_key": "device:device-a",
        "game_count": 4,
        "game_ids": ["g1", "g2", "g3", "g4"],
        "peer_principal_id": "p2",
        "principal_ids": ["p1", "p2"],
        "window_seconds": 600,
    }


def test_abnormal_win_streak_flags_binomial_tail_outlier() -> None:
    thresholds = RankedSignalThresholds(
        window_seconds=2_000,
        same_ip_min_principals=99,
        cooccurrence_min_games=99,
        streak_min_games=8,
        streak_max_tail_probability=0.01,
        bot_cadence_min_games=99,
    )
    rows = [
        _row(f"g{i}", "p-win", source_ip_hash=f"ip-{i}", observed_at=1_000 + i) for i in range(8)
    ]

    assert score_ranked_signals(rows[:-1], thresholds=thresholds, now=2_000) == []

    flags = score_ranked_signals(rows, thresholds=thresholds, now=2_000)

    assert [(flag.kind, flag.principal_id, flag.severity) for flag in flags] == [
        ("abnormal_win_streak", "p-win", "high")
    ]
    assert flags[0].evidence == {
        "game_count": 8,
        "losses": 0,
        "max_win_streak": 8,
        "tail_probability": "0.00390625",
        "wins": 8,
        "window_seconds": 2000,
    }


def test_bot_cadence_uses_precomputed_latency_features() -> None:
    thresholds = RankedSignalThresholds(
        window_seconds=600,
        same_ip_min_principals=99,
        cooccurrence_min_games=99,
        streak_min_games=99,
        bot_cadence_min_games=4,
        bot_cadence_max_average_latency_ms=600,
        bot_cadence_max_latency_spread_ms=15,
    )
    below = [
        _row(
            f"g{i}",
            "p-bot",
            source_ip_hash=f"ip-{i}",
            signals={"action_latency_ms": latency},
        )
        for i, latency in enumerate([500, 510, 505], start=1)
    ]

    assert score_ranked_signals(below, thresholds=thresholds, now=1_000) == []

    flags = score_ranked_signals(
        [*below, _row("g4", "p-bot", source_ip_hash="ip-4", signals={"action_latency_ms": 505})],
        thresholds=thresholds,
        now=1_000,
    )

    assert [(flag.kind, flag.principal_id, flag.severity) for flag in flags] == [
        ("bot_cadence", "p-bot", "medium")
    ]
    assert flags[0].evidence == {
        "average_latency_ms": 505,
        "game_count": 4,
        "latency_key": "action_latency_ms",
        "latency_spread_ms": 10,
        "sample_count": 4,
        "window_seconds": 600,
    }


def test_scoring_is_deterministic_independent_of_input_order() -> None:
    thresholds = RankedSignalThresholds(
        window_seconds=600,
        same_ip_min_principals=2,
        cooccurrence_min_games=99,
        streak_min_games=99,
        bot_cadence_min_games=99,
    )
    rows = [
        _row("g2", "p2", observed_at=990),
        _row("g1", "p1", observed_at=995),
    ]

    assert score_ranked_signals(rows, thresholds=thresholds, now=1_000) == score_ranked_signals(
        list(reversed(rows)),
        thresholds=thresholds,
        now=1_000,
    )


def test_signal_module_imports_no_random_and_reads_no_wall_clock() -> None:
    source = MODULE_PATH.read_text()
    assert "datetime.utcnow" not in source
    assert "datetime.now" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "random"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "random"
