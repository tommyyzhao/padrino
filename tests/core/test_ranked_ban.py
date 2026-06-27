"""US-300: pure ranked-ban predicate tests."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from padrino.core.ranked_ban import RankedBanKind, RankedBanSnapshot, is_ranked_banned

MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "padrino" / "core" / "ranked_ban.py"


def test_active_permanent_ban_is_ranked_banned() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert is_ranked_banned(
        [RankedBanSnapshot(kind=RankedBanKind.BAN, expires_at=None, lifted_at=None)],
        now,
    )


def test_expired_suspension_is_not_ranked_banned() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert not is_ranked_banned(
        [
            RankedBanSnapshot(
                kind=RankedBanKind.SUSPENSION,
                expires_at=now - timedelta(seconds=1),
                lifted_at=None,
            )
        ],
        now,
    )


def test_lifted_ban_or_suspension_is_not_ranked_banned() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert not is_ranked_banned(
        [
            RankedBanSnapshot(kind=RankedBanKind.BAN, expires_at=None, lifted_at=now),
            RankedBanSnapshot(
                kind=RankedBanKind.SUSPENSION,
                expires_at=now + timedelta(days=1),
                lifted_at=now,
            ),
        ],
        now,
    )


def test_future_expiry_suspension_is_ranked_banned() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert is_ranked_banned(
        [
            RankedBanSnapshot(
                kind=RankedBanKind.SUSPENSION,
                expires_at=now + timedelta(seconds=1),
                lifted_at=None,
            )
        ],
        now,
    )


def test_empty_or_boundary_expired_bans_are_not_ranked_banned() -> None:
    now = datetime(2026, 6, 27, tzinfo=UTC)

    assert not is_ranked_banned([], now)
    assert not is_ranked_banned(
        [
            RankedBanSnapshot(
                kind=RankedBanKind.SUSPENSION,
                expires_at=now,
                lifted_at=None,
            )
        ],
        now,
    )


def test_ranked_ban_module_has_no_wall_clock_or_rng_reads() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    offenders: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"random", "time", "secrets"}:
                    offenders.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in {"random", "time", "secrets"}:
                offenders.append((node.lineno, module))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"now", "utcnow", "time"}:
                offenders.append((node.lineno, func.attr))

    assert not offenders
