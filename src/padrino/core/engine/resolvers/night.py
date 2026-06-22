"""Night resolution composer.

Current ruleset night submissions are resolved by the formal NAR matrix in
``padrino.core.engine.resolvers.nar``. This module preserves the historical
``resolve_night`` import path.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.nar import MatrixNightResolution, resolve_current_night
from padrino.core.engine.state import GameState

NightResolution = MatrixNightResolution


def resolve_night(
    state: GameState,
    all_submissions: Mapping[str, Action],
) -> NightResolution:
    """Compose current night actions via the NAR matrix."""
    return resolve_current_night(state, all_submissions)
