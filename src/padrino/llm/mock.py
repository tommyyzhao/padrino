"""Deterministic mock LLM adapters for tests and demos.

Two adapters live here:

* :class:`DeterministicMockAdapter` — looks up a canned :class:`AgentResponse`
  by ``(phase_id, public_player_id)``. Raises on missing keys so integration
  tests fail loudly rather than silently substituting a default.
* :class:`NoopMockAdapter` — returns the engine-safe coercion fallback for
  every phase (``ABSTAIN`` on ``DAY_VOTE``, ``NOOP`` everywhere else) without
  any per-seat scripting. Used by the ``padrino demo-gauntlet`` CLI so a
  fresh checkout can run a full gauntlet without API keys or hand-rolled
  scripts.

Both adapters live in the impure ``llm`` layer; pure-core does not import them.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.agents.coercion import coerce_response_failure
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.state import Phase
from padrino.core.enums import PhaseKind
from padrino.core.observations import Observation
from padrino.llm.adapter import AdapterResult

_PHASE_KIND_BY_PREFIX: dict[str, PhaseKind] = {
    "SETUP": PhaseKind.SETUP,
    "TERMINAL": PhaseKind.TERMINAL,
    "NIGHT_0_MAFIA_INTRO": PhaseKind.NIGHT_0_MAFIA_INTRO,
}


class DeterministicMockAdapter:
    """Returns the scripted response keyed by ``(phase_id, player_id)``.

    Raises :class:`KeyError` on a missing key so tests fail loudly rather than
    silently substituting a default. Each invocation appends its lookup key to
    the public ``calls`` list for assertion.
    """

    __slots__ = ("_script", "calls")

    def __init__(self, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._script = dict(script)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        key = (observation.phase, observation.you.player_id)
        self.calls.append(key)
        response = self._script[key]
        return AdapterResult(
            raw_response=response.model_dump_json(),
            parsed_response=response,
            latency_ms=0,
        )


def _phase_from_observation(observation: Observation) -> Phase:
    """Reconstruct enough of :class:`Phase` to drive :func:`coerce_response_failure`."""
    return Phase(
        kind=_phase_kind_for(observation.phase),
        day=observation.day,
        round=observation.round,
    )


def _phase_kind_for(phase_id: str) -> PhaseKind:
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


class NoopMockAdapter:
    """Returns the safe-coercion response for every phase, no script needed.

    The response shape mirrors :func:`padrino.core.agents.coercion.coerce_response_failure`
    so every call yields an engine-legal ``AgentResponse``: ``ABSTAIN`` on
    ``DAY_VOTE`` and ``NOOP`` everywhere else. With this adapter every game
    runs to the ``MAX_DAYS_REACHED`` draw, which is enough to exercise the
    runner end-to-end without API keys or scripts.
    """

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        self.calls.append((observation.phase, observation.you.player_id))
        phase = _phase_from_observation(observation)
        response = coerce_response_failure(phase, "noop")
        return AdapterResult(
            raw_response=response.model_dump_json(),
            parsed_response=response,
            latency_ms=0,
        )


__all__ = ["DeterministicMockAdapter", "NoopMockAdapter"]
