"""US-310: pure strangers-only ranked lobby seating guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from padrino.core.lobby.strangers_only import (
    RANKED_REQUIRES_SINGLE_HUMAN_SEAT,
    RankedSeatingViolation,
    assert_strangers_only_ranked,
)


def test_ranked_lobby_rejects_multiple_human_seats() -> None:
    with pytest.raises(RankedSeatingViolation) as exc:
        assert_strangers_only_ranked(lobby_is_ranked=True, human_seat_count=2)

    assert exc.value.reason == RANKED_REQUIRES_SINGLE_HUMAN_SEAT


def test_ranked_lobby_accepts_exactly_one_human_seat() -> None:
    assert_strangers_only_ranked(lobby_is_ranked=True, human_seat_count=1)


def test_ranked_lobby_rejects_zero_human_seats() -> None:
    with pytest.raises(RankedSeatingViolation) as exc:
        assert_strangers_only_ranked(lobby_is_ranked=True, human_seat_count=0)

    assert exc.value.reason == RANKED_REQUIRES_SINGLE_HUMAN_SEAT


@pytest.mark.parametrize("human_seat_count", [0, 1, 2, 7])
def test_casual_lobby_allows_any_human_seat_count(human_seat_count: int) -> None:
    assert_strangers_only_ranked(lobby_is_ranked=False, human_seat_count=human_seat_count)


def test_guard_module_has_no_impure_imports() -> None:
    source = Path("src/padrino/core/lobby/strangers_only.py").read_text()
    for forbidden in ("sqlalchemy", "padrino.db", "datetime", "time", "random", "secrets"):
        assert forbidden not in source
