"""Human seat adapter satisfying the :class:`LlmAdapter` Protocol (US-137).

The runner's tick barrier drives every seat — AI or human — through the exact
same contract: ``await adapter.complete(observation) -> AdapterResult``. A human
seat is therefore just another adapter. :class:`HumanAdapter` resolves the
seat's turn from *buffered* human input (the authenticated POST action channel,
US-134) within the per-phase deadline, polling a caller-supplied
:data:`PullAction` source. The tick contract is unchanged: the human adapter
hides all of its deadline / polling behind the single ``complete`` method.

Three outcomes, mirroring a (mis)behaving LLM:

* **Buffered + legal** — the human's structured :class:`Action` is wrapped in an
  engine-legal :class:`AgentResponse` and returned (``status="ok"``).
* **Buffered + illegal** — an illegal type / target (validated against
  :func:`legal_actions_for`, surfaced on the observation as ``legal_actions``) is
  coerced to the engine-safe action exactly like a malformed LLM response
  (``status="schema_violation"``).
* **Timeout** — no buffered input by the deadline collapses to the engine-safe
  action exactly like an LLM that never answered (``status="provider_error"``).

The clock and sleep are injected so the polling loop is deterministic and never
touches the wall clock inside pure tests. This module lives in the impure
``llm`` layer; pure-core code never imports it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from padrino.core.agents.coercion import coerce_response_failure
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions, action_requires_target
from padrino.core.observations import Observation
from padrino.llm.adapter import AdapterResult, AdapterStatus
from padrino.llm.observation_phase import phase_from_observation

#: Async source of buffered human input for a seat's current phase. Returns the
#: latest structured :class:`Action` the human submitted (over the POST channel),
#: or ``None`` if nothing has been buffered yet.
PullAction = Callable[[Observation], Awaitable[Action | None]]

#: Monotonic clock (seconds). Injected so the polling loop is deterministic.
Clock = Callable[[], float]

#: Async sleep. Injected so tests advance a fake clock instead of blocking.
Sleep = Callable[[float], Awaitable[None]]

_DEFAULT_DEADLINE_SECONDS = 120.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.5

_REASON_TIMEOUT = "human_timeout"
_REASON_ILLEGAL = "human_illegal_submission"


def _is_legal(action: Action, legal: LegalActions) -> bool:
    """Return whether ``action`` is legal given the seat's ``legal_actions``."""
    if action.type not in legal.allowed_action_types:
        return False
    if action_requires_target(action.type):
        return action.target is not None and action.target in legal.legal_targets
    # NOOP / ABSTAIN must not carry a target.
    return action.target is None


class HumanAdapter:
    """Drive a human seat through the :class:`LlmAdapter` contract.

    Polls ``pull_action`` for buffered human input every ``poll_interval_seconds``
    until either a submission arrives or ``deadline_seconds`` elapses (measured on
    the injected monotonic ``clock``). A legal submission is returned verbatim; an
    illegal one or a deadline miss is coerced to the engine-safe action.
    """

    __slots__ = ("_clock", "_deadline_seconds", "_poll_interval_seconds", "_pull_action", "_sleep")

    def __init__(
        self,
        *,
        pull_action: PullAction,
        deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._pull_action = pull_action
        self._deadline_seconds = deadline_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleep = sleep

    async def complete(self, observation: Observation) -> AdapterResult:
        deadline = self._clock() + self._deadline_seconds

        while True:
            action = await self._pull_action(observation)
            if action is not None:
                return self._resolve(observation, action)

            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._timeout(observation)
            await self._sleep(min(self._poll_interval_seconds, remaining))

    def _resolve(self, observation: Observation, action: Action) -> AdapterResult:
        if _is_legal(action, observation.legal_actions):
            response = AgentResponse(
                public_message=None,
                private_message=None,
                action=action,
                memory_update="",
                rationale_summary=None,
            )
            return AdapterResult(
                raw_response=response.model_dump_json(),
                parsed_response=response,
                latency_ms=0,
                status="ok",
            )
        return self._coerce(observation, status="schema_violation", reason=_REASON_ILLEGAL)

    def _timeout(self, observation: Observation) -> AdapterResult:
        return self._coerce(observation, status="provider_error", reason=_REASON_TIMEOUT)

    def _coerce(
        self, observation: Observation, *, status: AdapterStatus, reason: str
    ) -> AdapterResult:
        phase = phase_from_observation(observation)
        response = coerce_response_failure(phase, reason)
        return AdapterResult(
            raw_response=response.model_dump_json(),
            parsed_response=response,
            latency_ms=0,
            status=status,
            error=reason,
        )


__all__ = ["Clock", "HumanAdapter", "PullAction", "Sleep"]
