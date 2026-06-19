"""Unit tests for :class:`SeatMultiplexAdapter` (US-083).

Verifies the multiplex dispatches each seat's observation to that seat's
assigned adapter, accumulates per-seat outcomes, and satisfies the
:class:`LlmAdapter` Protocol without touching any provider.
"""

from __future__ import annotations

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter

_PHASE = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
_SEATS: tuple[Seat, ...] = (
    Seat(
        public_player_id="P01",
        seat_index=0,
        role=Role.MAFIA_GOON,
        faction=Faction.MAFIA,
        alive=True,
    ),
    Seat(
        public_player_id="P02",
        seat_index=1,
        role=Role.MAFIA_GOON,
        faction=Faction.MAFIA,
        alive=True,
    ),
    Seat(
        public_player_id="P03", seat_index=2, role=Role.DETECTIVE, faction=Faction.TOWN, alive=True
    ),
    Seat(public_player_id="P04", seat_index=3, role=Role.DOCTOR, faction=Faction.TOWN, alive=True),
    Seat(
        public_player_id="P05", seat_index=4, role=Role.VILLAGER, faction=Faction.TOWN, alive=True
    ),
    Seat(
        public_player_id="P06", seat_index=5, role=Role.VILLAGER, faction=Faction.TOWN, alive=True
    ),
    Seat(
        public_player_id="P07", seat_index=6, role=Role.VILLAGER, faction=Faction.TOWN, alive=True
    ),
)


def _observation_for(seat: Seat) -> Observation:
    state = GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-MULTIPLEX",
        game_seed="seed-multiplex",
        current_phase=_PHASE,
        seats=_SEATS,
        day=_PHASE.day,
    )
    return build_observation(state, seat, EventLog(), mini7_v1)


class _TaggingAdapter:
    """Stub adapter that records seen seats and returns a tagged result."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.seen_seats: list[str] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        self.seen_seats.append(observation.you.player_id)
        return AdapterResult(
            raw_response=self.tag,
            parsed_response=AgentResponse(
                public_message=None,
                private_message=None,
                action=Action(type=ActionType.ABSTAIN, target=None),
                memory_update="",
                rationale_summary=None,
            ),
            latency_ms=1,
            status="ok",
        )


def test_multiplex_conforms_to_adapter_protocol() -> None:
    mux = SeatMultiplexAdapter({"P01": _TaggingAdapter("a")})
    assert isinstance(mux, LlmAdapter)


def test_empty_assignments_rejected() -> None:
    with pytest.raises(ValueError, match="at least one seat adapter"):
        SeatMultiplexAdapter({})


async def test_dispatches_each_seat_to_its_own_adapter() -> None:
    a = _TaggingAdapter("alpha")
    b = _TaggingAdapter("bravo")
    mux = SeatMultiplexAdapter({"P01": a, "P03": b})

    r1 = await mux.complete(_observation_for(_SEATS[0]))  # P01 -> a
    r3 = await mux.complete(_observation_for(_SEATS[2]))  # P03 -> b

    assert r1.raw_response == "alpha"
    assert r3.raw_response == "bravo"
    assert a.seen_seats == ["P01"]
    assert b.seen_seats == ["P03"]


async def test_accumulates_calls_by_seat() -> None:
    a = _TaggingAdapter("alpha")
    mux = SeatMultiplexAdapter({"P01": a})

    await mux.complete(_observation_for(_SEATS[0]))
    await mux.complete(_observation_for(_SEATS[0]))

    assert list(mux.calls_by_seat) == ["P01"]
    assert len(mux.calls_by_seat["P01"]) == 2
    assert all(c.status == "ok" for c in mux.calls_by_seat["P01"])


async def test_unknown_seat_raises_with_known_seats() -> None:
    mux = SeatMultiplexAdapter({"P01": _TaggingAdapter("a")})
    with pytest.raises(KeyError, match="P03"):
        await mux.complete(_observation_for(_SEATS[2]))


async def test_swap_seat_rebinds_adapter_and_returns_previous() -> None:
    # US-139: an AI takeover swaps a seat's adapter between ticks.
    human = _TaggingAdapter("human")
    takeover = _TaggingAdapter("ai")
    mux = SeatMultiplexAdapter({"P01": human})

    before = await mux.complete(_observation_for(_SEATS[0]))
    assert before.raw_response == "human"

    replaced = mux.swap_seat("P01", takeover)
    assert replaced is human

    after = await mux.complete(_observation_for(_SEATS[0]))
    assert after.raw_response == "ai"
    assert takeover.seen_seats == ["P01"]


def test_swap_unknown_seat_raises_with_known_seats() -> None:
    mux = SeatMultiplexAdapter({"P01": _TaggingAdapter("a")})
    with pytest.raises(KeyError, match="P03"):
        mux.swap_seat("P03", _TaggingAdapter("b"))
