"""Deterministic mock LLM adapter for integration tests.

`DeterministicMockAdapter` returns canned :class:`AgentResponse` payloads keyed
by ``(phase_id, public_player_id)``. The script is the full source of truth for
what each seat will say in each phase — no randomness, no network, no clock.

Used by integration tests (US-027+) to drive complete games without invoking a
real provider. Lives in the impure ``llm`` layer; pure-core does not import it.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.agents.contract import AgentResponse
from padrino.core.observations import Observation
from padrino.llm.adapter import AdapterResult


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


__all__ = ["DeterministicMockAdapter"]
