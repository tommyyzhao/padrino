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
