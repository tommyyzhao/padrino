"""Human-aware tick with a symmetric fixed public-message release delay (US-138).

A mixed human+AI game must not let *timing* out a seat. An LLM seat answers in
seconds; a human seat may take most of a human-friendly per-phase deadline. If
public messages were released the instant the tick resolved, a spectator could
infer which seats are human purely from when their words appeared. US-138 closes
that channel: :func:`run_human_tick` awaits every seat — human :class:`HumanAdapter`
and LLM alike — under one per-phase deadline, then applies a **single fixed
release delay symmetrically to ALL public messages** (human AND AI), so every
public message in a phase shares the *same* release schedule and no per-side
timing signal exists.

This is the v1 simplification of decision 5: a fixed delay, with **no** seeded
jitter and **no** multi-message release windows. AI messages wait in the buffer
too — the delay inverts the benchmark runner's "emit at tick resolution".

All delay / clock logic lives here in the impure runner; the pure core never
imports this module and the symmetric-release behaviour is driven entirely by an
injected monotonic clock and async sleep, so tests are deterministic and never
touch the wall clock. The underlying tick barrier (:func:`padrino.runner.tick.run_tick`)
is unchanged — its contract already coerces slow seats to a safe action, so the
human deadline is just the ``timeout_s`` passed through.

Impure runner module; pure-core code does not import it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import PhaseKind
from padrino.core.observations import Ruleset
from padrino.llm.adapter import LlmAdapter
from padrino.runner.tick import run_tick

#: Monotonic clock (seconds). Injected so the release schedule is deterministic.
Clock = Callable[[], float]

#: Async sleep. Injected so tests advance a fake clock instead of blocking.
Sleep = Callable[[float], Awaitable[None]]

# Public messages are only released during the day talk phases — mirrors the
# gating in :func:`padrino.runner.game_runner._submission_events_for`.
_PUBLIC_MESSAGE_PHASES = frozenset({PhaseKind.DAY_DISCUSSION, PhaseKind.DAY_VOTE})


@dataclass(frozen=True, slots=True)
class HumanTickConfig:
    """Human-friendly per-phase deadline and the symmetric release delay.

    Both values come from settings (``padrino_human_phase_deadline_seconds`` and
    ``padrino_human_release_delay_seconds``) at the runner seam; they are passed
    in as plain data so this module reads no settings and no wall clock itself.
    """

    phase_deadline_seconds: float
    release_delay_seconds: float


@dataclass(frozen=True, slots=True)
class ReleasedMessage:
    """One public message and the wall-clock instant it is released to viewers.

    ``released_at`` is identical for every message released in the same phase —
    that symmetry is the whole point: a human's words and an AI's words surface
    at exactly the same moment, so release timing reveals nothing.
    """

    seat_id: str
    text: str
    released_at: float


@dataclass(frozen=True, slots=True)
class HumanTickResult:
    """Outcome of one human-aware tick.

    ``responses`` is exactly what :func:`run_tick` returned (seat id -> coerced
    :class:`AgentResponse`), unchanged. ``released_messages`` are the phase's
    public messages, in seat order, each stamped with the common release instant.
    ``settled_at`` is the clock reading after the release delay elapsed — the
    phase always settles within ``deadline + release_delay`` of its start.
    """

    responses: dict[str, AgentResponse]
    released_messages: tuple[ReleasedMessage, ...] = field(default_factory=tuple)
    settled_at: float = 0.0


def _public_messages(
    eligible_seats: Sequence[Seat],
    responses: dict[str, AgentResponse],
    phase_kind: PhaseKind,
) -> list[tuple[str, str]]:
    """Collect ``(seat_id, text)`` for every released public message, in seat order.

    Identity-blind by construction: a human seat and an AI seat both surface their
    ``public_message`` the same way, so the caller cannot tell them apart from the
    returned list — only the seat order (already public) is preserved.
    """
    if phase_kind not in _PUBLIC_MESSAGE_PHASES:
        return []
    collected: list[tuple[str, str]] = []
    for seat in eligible_seats:
        response = responses.get(seat.public_player_id)
        if response is not None and response.public_message:
            collected.append((seat.public_player_id, response.public_message))
    return collected


async def run_human_tick(
    state: GameState,
    event_log: EventLog,
    eligible_seats: Sequence[Seat],
    adapter: LlmAdapter,
    ruleset: Ruleset,
    config: HumanTickConfig,
    *,
    ranked: bool = False,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> HumanTickResult:
    """Tick every seat under the human deadline, then release messages symmetrically.

    1. Dispatches all eligible seats (human + LLM) through :func:`run_tick` under
       ``config.phase_deadline_seconds`` — the same hard barrier the benchmark
       runner uses, so a slow human is coerced to a safe action exactly like a
       slow LLM. The barrier returns only once every seat has settled.
    2. Holds the resulting public messages and releases them after a *single*
       fixed ``config.release_delay_seconds`` measured on the injected ``clock``,
       so AI and human public messages share the same release instant.

    The phase always settles within ``deadline + release_delay`` of the tick's
    start: the barrier returns within ``deadline`` and the release adds exactly
    ``release_delay``.
    """
    responses = await run_tick(
        state,
        event_log,
        eligible_seats,
        adapter,
        timeout_s=config.phase_deadline_seconds,
        ruleset=ruleset,
        ranked=ranked,
    )

    pending = _public_messages(eligible_seats, responses, state.current_phase.kind)

    # Symmetric hold: one fixed delay for the whole phase, applied to AI and human
    # messages alike. The release instant is computed AFTER sleeping so it reflects
    # the real clock the viewer-facing layer will see.
    delay = max(config.release_delay_seconds, 0.0)
    if delay > 0:
        await sleep(delay)
    released_at = clock()

    released = tuple(
        ReleasedMessage(seat_id=seat_id, text=text, released_at=released_at)
        for seat_id, text in pending
    )
    return HumanTickResult(
        responses=responses,
        released_messages=released,
        settled_at=released_at,
    )


__all__ = [
    "Clock",
    "HumanTickConfig",
    "HumanTickResult",
    "ReleasedMessage",
    "Sleep",
    "run_human_tick",
]
