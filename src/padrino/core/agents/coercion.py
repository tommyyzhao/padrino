"""Schema-failure coercion to engine-safe actions and responses.

When an agent response fails to parse, fails schema validation, or times out,
the runner needs a deterministic, engine-legal fallback so a broken model can
never crash a game. This module exposes two pure helpers:

- :func:`coerce_to_safe_action` returns ``ABSTAIN`` during the day vote phase
  and ``NOOP`` everywhere else.
- :func:`coerce_response_failure` wraps that safe action in a fully-populated
  :class:`AgentResponse` with all optional fields zeroed out.

The ``error_reason`` argument is accepted for caller convenience (and so the
runner can log a single string through both helpers) but does not influence
the returned action — coercion is a function of the phase alone.

Pure core: no DB / LLM / clock / network / random imports.
"""

from __future__ import annotations

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.state import Phase
from padrino.core.enums import ActionType, PhaseKind


def coerce_to_safe_action(phase: Phase, error_reason: str) -> Action:
    """Return the engine-legal fallback action for ``phase``.

    ``DAY_VOTE`` collapses to ``ABSTAIN``; every other phase collapses to
    ``NOOP``. ``error_reason`` is ignored — included for symmetry with
    :func:`coerce_response_failure` and to keep call sites readable.
    """

    if phase.kind is PhaseKind.DAY_VOTE:
        return Action(type=ActionType.ABSTAIN, target=None)
    return Action(type=ActionType.NOOP, target=None)


def coerce_response_failure(phase: Phase, error_reason: str) -> AgentResponse:
    """Wrap :func:`coerce_to_safe_action` in a zeroed :class:`AgentResponse`."""

    return AgentResponse(
        public_message=None,
        private_message=None,
        action=coerce_to_safe_action(phase, error_reason),
        memory_update="",
        rationale_summary=None,
    )
