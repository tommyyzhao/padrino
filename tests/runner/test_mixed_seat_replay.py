"""Mixed human+AI seats replay bit-for-bit (US-139).

A single game may seat a per-seat mix of :class:`~padrino.llm.human_adapter.HumanAdapter`
and LLM adapters, wired through one :class:`~padrino.llm.multiplex.SeatMultiplexAdapter`,
and an AI takeover may swap a seat's adapter *between* ticks. This module proves
the load-bearing properties:

* **The mix is transparent to the engine.** Driving a full game where some seats
  are human (resolving their turns from a buffered submission source) and the rest
  are scripted LLM adapters produces a hash-chained event log that replays to an
  identical state; the same game driven fully-AI yields a byte-identical chain, so
  the human/AI distinction never reaches the engine or the log.
* **A takeover swaps the adapter between ticks and stays replay-stable.** All human
  non-determinism (which action a human submitted, release ordering, the takeover
  itself) is captured as committed events; the adapter swap holds no game state. A
  game that swaps a human seat to an AI mid-play — committing a single
  ``SeatTakenOver`` event — replays to the same hash-chained state, and the
  taken-over seat reconstructs HUMAN_THEN_AI provenance from the committed log.

Clocks and RNG stay in the runner: the :class:`HumanAdapter` polls a deterministic
buffer under an injected clock, so no test touches the wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import Event, EventAdapter
from padrino.core.engine.reducer import compute_seat_provenance
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-mixed-001"


class _FakeClock:
    """Monotonic clock advancing only when the injected ``sleep`` is awaited."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _ScriptedSeatAdapter:
    """An LLM adapter returning one seat's slice of a phase-keyed script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: resp for key, resp in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


def _human_adapter(
    seat_id: str,
    script: Mapping[tuple[str, str], AgentResponse],
    clock: _FakeClock,
) -> HumanAdapter:
    """A human seat whose buffered submission is the seat's scripted action.

    The ``pull_action`` source mirrors the authenticated POST channel: it hands
    back the seat's structured :class:`Action` for the current phase. Because the
    action is identical to the one a scripted LLM would emit, the game resolves
    the same regardless of whether the seat is human or AI — that is the whole
    point of the mix being engine-transparent.
    """

    async def pull(observation: Observation) -> Action | None:
        response = script[(observation.phase, seat_id)]
        return response.action

    return HumanAdapter(
        pull_action=pull,
        deadline_seconds=30.0,
        poll_interval_seconds=0.5,
        clock=clock.now,
        sleep=clock.sleep,
    )


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


def _config() -> GameConfig:
    return GameConfig(game_id="G-MIXED", game_seed=_GAME_SEED, timeout_s=1.0)


def _typed_events(outcome: GameOutcome) -> list[Event]:
    return [EventAdapter.validate_python(stored.body) for stored in outcome.event_log.events]


def _mixed_adapter(
    human_seats: set[str],
    script: Mapping[tuple[str, str], AgentResponse],
    clock: _FakeClock,
) -> SeatMultiplexAdapter:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    adapters: dict[str, LlmAdapter] = {}
    for seat in seats:
        sid = seat.public_player_id
        if sid in human_seats:
            adapters[sid] = _human_adapter(sid, script, clock)
        else:
            adapters[sid] = _ScriptedSeatAdapter(sid, script)
    return SeatMultiplexAdapter(adapters)


async def test_mixed_human_ai_game_replays_to_identical_state() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    clock = _FakeClock()
    # A representative mix: one town human and one mafia human; the rest are AI.
    human_seats = {town[0], mafia[0]}
    mux = _mixed_adapter(human_seats, script, clock)

    outcome = await run_game(_config(), mux, ranked=False)

    assert outcome.final_state.terminal_result == "TOWN"

    # The hash chain replays cleanly: a mixed-seat game is byte-identical to fold.
    replayed = replay_event_log(outcome.event_log.events)
    assert len(replayed.events) == len(outcome.event_log.events)
    for original, repeated in zip(outcome.event_log.events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.prev_event_hash == repeated.prev_event_hash
        assert original.sequence == repeated.sequence

    # And the reducer fold over the typed stream reproduces the final state.
    replayed_state = replay_events(_typed_events(outcome))
    assert replayed_state.terminal_result == outcome.final_state.terminal_result
    assert replayed_state.seats == outcome.final_state.seats


async def test_human_and_ai_seats_produce_identical_log_for_same_actions() -> None:
    # Two runs of the same scripted game — one fully AI, one with a human-driven
    # subset of seats — must produce byte-identical hash chains, proving the
    # human/AI distinction never reaches the engine or the log.
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )

    all_ai = _mixed_adapter(set(), script, _FakeClock())
    mixed = _mixed_adapter({town[0], town[1], mafia[1]}, script, _FakeClock())

    ai_outcome = await run_game(_config(), all_ai, ranked=False)
    mixed_outcome = await run_game(_config(), mixed, ranked=False)

    ai_hashes = [e.event_hash for e in ai_outcome.event_log.events]
    mixed_hashes = [e.event_hash for e in mixed_outcome.event_log.events]
    assert ai_hashes == mixed_hashes


async def test_takeover_swaps_adapter_between_ticks_and_replays_identically() -> None:
    # A human seat is taken over by an AI mid-game: the mux swaps that seat's
    # adapter between ticks and a single SeatTakenOver event is committed. The
    # completed log must replay bit-for-bit.
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    clock = _FakeClock()
    taken_seat = town[0]
    mux = _mixed_adapter({taken_seat}, script, clock)
    ai_replacement = _ScriptedSeatAdapter(taken_seat, script)

    class _TakeoverMux:
        """Wrap the mux so a takeover fires once, between two ticks."""

        def __init__(self) -> None:
            self._swapped = False
            self.recorded_swap: LlmAdapter | None = None

        async def complete(self, observation: Observation) -> AdapterResult:
            # Trigger the swap the first time ANY seat is asked during the day-1
            # vote phase — i.e. between ticks, never mid-call for the taken seat.
            if not self._swapped and observation.phase.startswith("DAY_1_VOTE"):
                self.recorded_swap = mux.swap_seat(taken_seat, ai_replacement)
                self._swapped = True
            return await mux.complete(observation)

    takeover_mux = _TakeoverMux()
    outcome = await run_game(_config(), takeover_mux, ranked=False)

    # The swap actually happened and returned the human adapter it replaced.
    assert isinstance(takeover_mux.recorded_swap, HumanAdapter)

    # Commit the canonical SeatTakenOver event into the completed log. It folds as
    # provenance-only (state-preserving) and must keep the chain valid.
    log = outcome.event_log
    takeover_phase = "DAY_1_VOTE"
    log.append(
        {
            "event_type": "SeatTakenOver",
            "sequence": len(log.events),
            "phase": takeover_phase,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "public_player_id": taken_seat,
                "day": 1,
                "phase": takeover_phase,
                "reason": "disconnect_grace_expired",
                "replacement_agent_build_ref": "curated-autofill",
            },
        }
    )

    # The whole log — including the takeover event — replays bit-for-bit, and the
    # SeatTakenOver fold preserves the pre-takeover terminal state.
    replayed = replay_event_log(log.events)
    for original, repeated in zip(log.events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.sequence == repeated.sequence
    folded = replay_events([EventAdapter.validate_python(e.body) for e in log.events])
    assert folded.terminal_result == outcome.final_state.terminal_result
    assert folded.seats == outcome.final_state.seats


async def test_human_then_ai_provenance_from_committed_takeover() -> None:
    # When the taken-over seat was marked HUMAN at assignment, a committed
    # SeatTakenOver reconstructs HUMAN_THEN_AI provenance — purely from the log.
    log = EventLog()
    log.append(
        {
            "event_type": "RolesAssigned",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "assignments": [
                    {
                        "public_player_id": "P01",
                        "seat_index": 0,
                        "role": "VILLAGER",
                        "faction": "TOWN",
                        "seat_kind": "HUMAN",
                    },
                    {
                        "public_player_id": "P02",
                        "seat_index": 1,
                        "role": "MAFIA_GOON",
                        "faction": "MAFIA",
                        "seat_kind": "AI",
                    },
                ]
            },
        }
    )
    log.append(
        {
            "event_type": "SeatTakenOver",
            "sequence": 1,
            "phase": "DAY_1_VOTE",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "public_player_id": "P01",
                "day": 1,
                "phase": "DAY_1_VOTE",
                "reason": "disconnect_grace_expired",
                "replacement_agent_build_ref": "curated-autofill",
            },
        }
    )

    # Chain is valid after the takeover event.
    replay_event_log(log.events)

    typed = [EventAdapter.validate_python(e.body) for e in log.events]
    provenance = compute_seat_provenance(typed)
    assert provenance["P01"] == "HUMAN_THEN_AI"
    assert provenance["P02"] == "AI"
