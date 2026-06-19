"""Pure lobby helpers (Wave 9 human multiplayer)."""

from __future__ import annotations

from padrino.core.lobby.autofill import (
    AutoFillAssignment,
    NotEnoughCuratedModelsError,
    autofill_empty_seats,
)

__all__ = [
    "AutoFillAssignment",
    "NotEnoughCuratedModelsError",
    "autofill_empty_seats",
]
