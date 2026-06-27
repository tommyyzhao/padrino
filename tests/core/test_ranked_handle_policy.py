"""Ranked handle policy tests (US-297)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from padrino.core.ranked_handle_policy import (
    RankedHandleReason,
    validate_ranked_handle,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "padrino" / "core" / "ranked_handle_policy.py"
)


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("ab", RankedHandleReason.TOO_SHORT),
        ("a" * 25, RankedHandleReason.TOO_LONG),
        ("bad handle", RankedHandleReason.BAD_CHARSET),
        ("foulplayer", RankedHandleReason.PROFANE),
        ("PadrinoOfficial", RankedHandleReason.RESERVED),
        ("Admin_7", RankedHandleReason.RESERVED),
    ],
)
def test_ranked_handle_validator_returns_reason_tokens(
    candidate: str,
    reason: RankedHandleReason,
) -> None:
    decision = validate_ranked_handle(
        candidate,
        blocklist=("foul",),
        reserved=("admin", "padrino"),
    )

    assert decision.ok is False
    assert decision.reason is reason
    assert isinstance(decision.reason, str)


def test_ranked_handle_validator_accepts_and_normalizes_case_insensitively() -> None:
    decision = validate_ranked_handle(
        "Ranked_Hero-42",
        blocklist=(),
        reserved=(),
    )

    assert decision.ok is True
    assert decision.reason is RankedHandleReason.OK
    assert decision.handle == "Ranked_Hero-42"
    assert decision.normalized == "ranked_hero-42"


def test_profanity_and_reserved_lists_are_injected() -> None:
    assert validate_ranked_handle("FoulPlayer", blocklist=(), reserved=()).ok is True
    assert (
        validate_ranked_handle("FoulPlayer", blocklist=("foul",), reserved=()).reason
        is RankedHandleReason.PROFANE
    )
    assert validate_ranked_handle("Admin", blocklist=(), reserved=()).ok is True
    assert (
        validate_ranked_handle("Admin", blocklist=(), reserved=("admin",)).reason
        is RankedHandleReason.RESERVED
    )


def test_ranked_handle_policy_module_has_no_impure_imports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    offenders: list[tuple[int, str]] = []
    forbidden_exact = {"random", "datetime", "time", "secrets"}
    forbidden_prefixes = ("padrino.db", "padrino.settings")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""]
        else:
            continue
        for module in imported:
            if module in forbidden_exact or module.startswith(forbidden_prefixes):
                offenders.append((node.lineno, module))

    assert not offenders
