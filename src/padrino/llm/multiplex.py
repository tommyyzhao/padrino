"""Per-seat multiplexing adapter for heterogeneous gauntlets (US-083).

A normal gauntlet game clones ONE model across all seven seats. The
:class:`SeatMultiplexAdapter` wraps one concrete :class:`LlmAdapter` per
public player id and dispatches each :meth:`complete` call to the adapter
assigned to ``observation.you.player_id``. That is the seam that lets a
single game seat seven DISTINCT model identities — turning the gauntlet
from a self-play stress test into a head-to-head benchmark.

The multiplex satisfies the :class:`LlmAdapter` Protocol, so the runner
treats a heterogeneous roster exactly like a single-model game; the
recording layer (``_RecordingAdapter``) only ever calls ``complete`` and
consumes the returned :class:`AdapterResult`.

Per-seat call outcomes are accumulated in :attr:`calls_by_seat` so callers
can assert per-model coverage (every distinct model produced at least one
parsed-OK call) without re-deriving seat attribution from the flat,
seat-anonymous outcome log — :class:`AdapterResult` carries no seat id.

The same per-seat dispatch is the seam a *mixed human+AI* game rides on
(US-139): a seat may hold a :class:`~padrino.llm.human_adapter.HumanAdapter`
or any LLM adapter, and :meth:`swap_seat` rebinds a single seat's adapter
**between ticks** so a silent AI takeover (paired with a committed
``SeatTakenOver`` event) changes who drives the seat without touching the
tick barrier. All non-determinism — which human submitted what, release
ordering, the takeover itself — is captured by the runner as committed
events; the adapter swap holds no game state and so never perturbs replay.

Impure ``llm`` layer; pure-core never imports it.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.observations import Observation
from padrino.llm.adapter import AdapterResult, LlmAdapter


class SeatMultiplexAdapter:
    """Dispatch each seat's observation to that seat's assigned adapter."""

    __slots__ = ("_adapters", "calls_by_seat")

    def __init__(self, adapters: Mapping[str, LlmAdapter]) -> None:
        if not adapters:
            raise ValueError("SeatMultiplexAdapter requires at least one seat adapter")
        self._adapters: dict[str, LlmAdapter] = dict(adapters)
        # seat public_player_id -> ordered results that seat's adapter produced.
        self.calls_by_seat: dict[str, list[AdapterResult]] = {}

    def swap_seat(self, seat: str, adapter: LlmAdapter) -> LlmAdapter:
        """Rebind ``seat``'s adapter and return the one it replaced (US-139).

        This is the mechanism an AI takeover uses: between two ticks the
        runner swaps a human seat's :class:`~padrino.llm.human_adapter.HumanAdapter`
        for a curated LLM adapter, then commits a ``SeatTakenOver`` event so the
        change is replay-reconstructable. The swap mutates only the dispatch
        table — no game state lives here — so replaying the committed log
        reproduces an identical hash-chained state regardless of when seats were
        swapped. The seat must already be known (a takeover replaces an existing
        occupant; it never introduces a new seat).
        """
        if seat not in self._adapters:
            raise KeyError(
                f"SeatMultiplexAdapter cannot swap unknown seat {seat!r}; "
                f"known seats={sorted(self._adapters)}"
            )
        previous = self._adapters[seat]
        self._adapters[seat] = adapter
        return previous

    def force_swap_seat(self, seat: str, adapter: LlmAdapter) -> LlmAdapter | None:
        """Rebind ``seat`` even if normal swap validation failed post-commit.

        This is reserved for crash-consistency recovery after a takeover row has
        already committed to ``game_events``. The DB row is now authoritative, so
        the worker must make the in-memory dispatch table match it before any
        later tick can build another event.
        """
        previous = self._adapters.get(seat)
        self._adapters[seat] = adapter
        return previous

    async def complete(self, observation: Observation) -> AdapterResult:
        seat = observation.you.player_id
        try:
            adapter = self._adapters[seat]
        except KeyError as exc:
            raise KeyError(
                f"SeatMultiplexAdapter has no adapter for seat {seat!r}; "
                f"known seats={sorted(self._adapters)}"
            ) from exc
        result = await adapter.complete(observation)
        self.calls_by_seat.setdefault(seat, []).append(result)
        return result


__all__ = ["SeatMultiplexAdapter"]
