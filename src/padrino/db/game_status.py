"""Shared persisted game-status literals."""

from __future__ import annotations

from typing import Final, Literal

GameStatus = Literal["CREATED", "RUNNING", "COMPLETED", "FAILED"]

GAME_STATUS_CREATED: Final[GameStatus] = "CREATED"
GAME_STATUS_RUNNING: Final[GameStatus] = "RUNNING"
GAME_STATUS_COMPLETED: Final[GameStatus] = "COMPLETED"
GAME_STATUS_FAILED: Final[GameStatus] = "FAILED"

GAME_TERMINAL_STATUSES: Final[frozenset[GameStatus]] = frozenset(
    {GAME_STATUS_COMPLETED, GAME_STATUS_FAILED}
)


def is_terminal_game_status(status: str) -> bool:
    """Return whether ``status`` is terminal for scheduler selection."""
    return status in GAME_TERMINAL_STATUSES


__all__ = [
    "GAME_STATUS_COMPLETED",
    "GAME_STATUS_CREATED",
    "GAME_STATUS_FAILED",
    "GAME_STATUS_RUNNING",
    "GAME_TERMINAL_STATUSES",
    "GameStatus",
    "is_terminal_game_status",
]
