"""Reconstruct a core :class:`Phase` from an :class:`Observation`.

Both the deterministic mock adapter (US-025) and the human adapter (US-137)
need to drive :func:`padrino.core.agents.coercion.coerce_response_failure`, which
is keyed on the phase *kind* (``DAY_VOTE`` collapses to ``ABSTAIN``; everything
else to ``NOOP``). The observation only carries the canonical phase *id* string,
so this module maps that id back to a :class:`Phase` with the right kind / day /
round. Centralising it keeps the two adapters from drifting apart.

This module sits in the impure ``llm`` layer; pure-core code never imports it.
"""

from __future__ import annotations

from padrino.core.engine.state import Phase
from padrino.core.enums import PhaseKind
from padrino.core.observations import Observation

_PHASE_KIND_BY_PREFIX: dict[str, PhaseKind] = {
    "SETUP": PhaseKind.SETUP,
    "TERMINAL": PhaseKind.TERMINAL,
    "NIGHT_0_MAFIA_INTRO": PhaseKind.NIGHT_0_MAFIA_INTRO,
}


def phase_kind_for(phase_id: str) -> PhaseKind:
    """Map a canonical phase-id string back to its :class:`PhaseKind`."""
    if phase_id in _PHASE_KIND_BY_PREFIX:
        return _PHASE_KIND_BY_PREFIX[phase_id]
    if "_DISCUSSION_ROUND_" in phase_id:
        return PhaseKind.DAY_DISCUSSION
    if phase_id.endswith("_VOTE"):
        return PhaseKind.DAY_VOTE
    if phase_id.endswith("_MAFIA_DISCUSSION"):
        return PhaseKind.NIGHT_MAFIA_DISCUSSION
    if phase_id.endswith("_ACTIONS"):
        return PhaseKind.NIGHT_ACTIONS
    raise ValueError(f"unrecognized phase id: {phase_id!r}")


def phase_from_observation(observation: Observation) -> Phase:
    """Reconstruct enough of :class:`Phase` to drive coercion helpers."""
    return Phase(
        kind=phase_kind_for(observation.phase),
        day=observation.day,
        round=observation.round,
    )


__all__ = ["phase_from_observation", "phase_kind_for"]
