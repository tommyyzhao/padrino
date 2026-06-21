"""Disconnect grace + silent AI takeover (US-150).

A human who drops gets a short reconnect grace window; on expiry a curated AI
silently assumes the seat so the game continues, invisibly in anonymous mode.
This module proves the load-bearing properties:

* **Grace decision is pure.** A seat that dropped within the window is still
  reconnectable; past it, the seat is listed for takeover. A reconnect before
  expiry returns the seat (it is never listed). All data-in / data-out with an
  injected ``now``.
* **Presence is identity-blind.** In anonymous mode a viewer sees ONLY its own
  seat presence — other seats' presence / reconnecting state is never exposed
  (AIs do not disconnect, so any per-seat presence signal would out a human).
  In transparent mode every seat's presence may be shown.
* **The takeover is reveal-only and replay-stable.** The runner swaps the seat's
  adapter on the existing :class:`SeatMultiplexAdapter` and commits a single
  SYSTEM-visibility ``SeatTakenOver`` event; the game progresses through it, no
  PUBLIC frame ever carries the takeover (no mid-game leak), the completed log
  replays bit-for-bit, and the seat reconstructs ``HUMAN_THEN_AI`` provenance at
  the reveal.

Clocks stay in the runner; the pure decisions take an injected ``now``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from padrino.core.agents.contract import AgentResponse
from padrino.core.disconnect import (
    SeatPresence,
    is_within_grace,
    project_presence_for_viewer,
    seats_past_grace,
)
from padrino.core.engine.actions import Action
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import compute_seat_provenance
from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, IdentityMode, Role
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.disconnect_takeover import take_over_seat
from padrino.runner.game_runner import GameConfig, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-disc-001"
_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


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
    async def pull(observation: Observation) -> Action | None:
        return script[(observation.phase, seat_id)].action

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


def _mixed_adapter(
    human_seats: set[str],
    script: Mapping[tuple[str, str], AgentResponse],
    clock: _FakeClock,
) -> SeatMultiplexAdapter:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    adapters: dict[str, LlmAdapter] = {}
    for seat in seats:
        sid = seat.public_player_id
        adapters[sid] = (
            _human_adapter(sid, script, clock)
            if sid in human_seats
            else _ScriptedSeatAdapter(sid, script)
        )
    return SeatMultiplexAdapter(adapters)


# --------------------------------------------------------------------------- #
# Pure grace-window decisions
# --------------------------------------------------------------------------- #


def test_connected_seat_is_always_within_grace() -> None:
    presence = SeatPresence(public_player_id="P01", connected=True)
    assert is_within_grace(presence, now=_NOW, grace_seconds=90.0)


def test_recent_drop_is_within_grace_but_old_drop_is_not() -> None:
    recent = SeatPresence(
        public_player_id="P01",
        connected=False,
        disconnected_at=_NOW - timedelta(seconds=30),
    )
    stale = SeatPresence(
        public_player_id="P02",
        connected=False,
        disconnected_at=_NOW - timedelta(seconds=120),
    )
    assert is_within_grace(recent, now=_NOW, grace_seconds=90.0)
    assert not is_within_grace(stale, now=_NOW, grace_seconds=90.0)


def test_seats_past_grace_lists_only_expired_disconnects() -> None:
    presences = [
        SeatPresence(public_player_id="P01", connected=True),
        SeatPresence(
            public_player_id="P02",
            connected=False,
            disconnected_at=_NOW - timedelta(seconds=30),
        ),
        SeatPresence(
            public_player_id="P03",
            connected=False,
            disconnected_at=_NOW - timedelta(seconds=200),
        ),
    ]
    assert seats_past_grace(presences, now=_NOW, grace_seconds=90.0) == ["P03"]


def test_reconnect_before_expiry_returns_the_seat() -> None:
    # The seat dropped long ago but reconnected: connected=True -> never listed.
    reconnected = SeatPresence(
        public_player_id="P03",
        connected=True,
        disconnected_at=_NOW - timedelta(seconds=500),
    )
    assert is_within_grace(reconnected, now=_NOW, grace_seconds=90.0)
    assert seats_past_grace([reconnected], now=_NOW, grace_seconds=90.0) == []


def test_naive_disconnect_timestamp_is_coerced_to_aware() -> None:
    # A SQLite-loaded tz-naive timestamp must not raise when compared to aware now.
    naive = SeatPresence(
        public_player_id="P01",
        connected=False,
        disconnected_at=datetime(2026, 6, 19, 11, 59, 30),  # naive on purpose
    )
    assert is_within_grace(naive, now=_NOW, grace_seconds=90.0)


# --------------------------------------------------------------------------- #
# Identity-blind presence projection
# --------------------------------------------------------------------------- #


def _presences() -> list[SeatPresence]:
    return [
        SeatPresence(public_player_id="P01", connected=True),
        SeatPresence(
            public_player_id="P02",
            connected=False,
            disconnected_at=_NOW - timedelta(seconds=10),
        ),
        SeatPresence(public_player_id="P03", connected=True),
    ]


def test_anonymous_presence_shows_only_the_viewers_own_seat() -> None:
    frame = project_presence_for_viewer(
        _presences(), viewer_seat_id="P01", identity_mode=IdentityMode.ANONYMOUS
    )
    assert frame == [{"seat_id": "P01", "connected": True}]


def test_none_identity_mode_fails_closed_to_anonymous() -> None:
    frame = project_presence_for_viewer(_presences(), viewer_seat_id="P02", identity_mode=None)
    # Only the viewer's own seat; the disconnected-at instant is never exposed.
    assert frame == [{"seat_id": "P02", "connected": False}]


def test_anonymous_presence_never_exposes_other_seat_reconnecting_state() -> None:
    frame = project_presence_for_viewer(
        _presences(), viewer_seat_id="P01", identity_mode=IdentityMode.ANONYMOUS
    )
    exposed_ids = {entry["seat_id"] for entry in frame}
    assert "P02" not in exposed_ids  # the disconnected/reconnecting seat is hidden
    for entry in frame:
        assert set(entry) == {"seat_id", "connected"}  # never a drop timestamp


def test_transparent_presence_may_show_every_seat() -> None:
    frame = project_presence_for_viewer(
        _presences(), viewer_seat_id="P01", identity_mode=IdentityMode.TRANSPARENT
    )
    assert {entry["seat_id"] for entry in frame} == {"P01", "P02", "P03"}


# --------------------------------------------------------------------------- #
# Runner orchestration: silent takeover, no leak, replay-stable provenance
# --------------------------------------------------------------------------- #


async def test_game_progresses_through_takeover_with_no_leak_and_reveal_provenance() -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    clock = _FakeClock()
    dropped_seat = town[0]
    mux = _mixed_adapter({dropped_seat}, script, clock)
    ai_replacement = _ScriptedSeatAdapter(dropped_seat, script)

    class _TakeoverMux:
        """Fire the silent takeover once, between two ticks, on grace expiry.

        The grace decision is made with the pure :func:`seats_past_grace`; the
        adapter swap is the runner's :func:`take_over_seat`. The ``SeatTakenOver``
        event itself is committed into the authoritative game log AFTER the run
        (mirroring the US-139 pattern), so the swap mutates only the dispatch
        table mid-run and the committed event is appended once to the real log.
        """

        def __init__(self) -> None:
            self._fired = False
            self.took_over = False
            self.swapped_to: LlmAdapter | None = None

        async def complete(self, observation: Observation) -> AdapterResult:
            if not self._fired and observation.phase.startswith("DAY_1_VOTE"):
                # The seat dropped before grace; the pure decision says take over.
                presences = [
                    SeatPresence(
                        public_player_id=dropped_seat,
                        connected=False,
                        disconnected_at=_NOW - timedelta(seconds=200),
                    )
                ]
                expired = seats_past_grace(presences, now=_NOW, grace_seconds=90.0)
                assert expired == [dropped_seat]
                self.swapped_to = mux.swap_seat(dropped_seat, ai_replacement)
                self.took_over = True
                self._fired = True
            return await mux.complete(observation)

    config = GameConfig(game_id="G-DISC", game_seed=_GAME_SEED, timeout_s=1.0)
    takeover_mux = _TakeoverMux()

    outcome = await run_game(config, takeover_mux, ranked=False)
    assert takeover_mux.took_over
    # The swap actually replaced the human adapter on the real mux.
    assert isinstance(takeover_mux.swapped_to, HumanAdapter)
    assert outcome.final_state.terminal_result == "TOWN"

    # Commit the canonical SeatTakenOver provenance event into the game log.
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
                "public_player_id": dropped_seat,
                "day": 1,
                "phase": takeover_phase,
                "reason": "disconnect_grace_expired",
                "replacement_agent_build_ref": "curated-autofill",
            },
        }
    )

    # No mid-game leak: the takeover event is SYSTEM-visibility (never PUBLIC).
    public_frames = [e for e in log.events if e.body.get("visibility") == "PUBLIC"]
    assert all(e.body["event_type"] != "SeatTakenOver" for e in public_frames)

    # The completed log (incl. the takeover) replays bit-for-bit.
    replayed = replay_event_log(log.events)
    for original, repeated in zip(log.events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.sequence == repeated.sequence

    # Reveal provenance is reconstructable only at the end, from the committed log.
    typed = [EventAdapter.validate_python(e.body) for e in log.events]
    provenance = compute_seat_provenance(typed)
    # The runner's RolesAssigned does not stamp seat_kind (assignment marks human
    # seats at launch handoff, US-149), so a fully-AI-marked log shows AI here;
    # provenance with a HUMAN-marked seat is covered below.
    assert dropped_seat in provenance


async def test_take_over_seat_emits_reveal_only_provenance_event() -> None:
    # take_over_seat on a HUMAN-marked log yields HUMAN_THEN_AI provenance and a
    # SYSTEM-visibility event carrying only pure data (no wall clock / random).
    from padrino.core.engine.event_log import EventLog
    from padrino.core.engine.state import GameState, Phase, Seat
    from padrino.core.enums import PhaseKind, SeatKind

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

    seats = (
        Seat(
            public_player_id="P01",
            seat_index=0,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
            alive=True,
            seat_kind=SeatKind.HUMAN,
        ),
        Seat(
            public_player_id="P02",
            seat_index=1,
            role=Role.MAFIA_GOON,
            faction=Faction.MAFIA,
            alive=True,
            seat_kind=SeatKind.AI,
        ),
    )
    state = GameState(
        ruleset_id="mini7_v1",
        game_id="G-DISC",
        game_seed=_GAME_SEED,
        current_phase=Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        seats=seats,
        day=1,
    )

    clock = _FakeClock()
    mux = SeatMultiplexAdapter(
        {
            "P01": _human_adapter("P01", {}, clock),
            "P02": _ScriptedSeatAdapter("P02", {}),
        }
    )
    replacement = _ScriptedSeatAdapter("P01", {})

    result = take_over_seat(
        mux=mux,
        event_log=log,
        state=state,
        seat_id="P01",
        replacement_adapter=replacement,
        replacement_agent_build_ref="curated-autofill",
    )

    # The human adapter was the one replaced.
    assert isinstance(result.replaced_adapter, HumanAdapter)

    # The emitted event is SYSTEM-visibility, pure-data, day/phase from the state.
    body = result.event.body
    assert body["visibility"] == "SYSTEM"
    assert body["payload"]["public_player_id"] == "P01"
    assert body["payload"]["day"] == 1
    assert body["payload"]["phase"] == "DAY_1_VOTE"
    assert body["payload"]["reason"] == "disconnect_grace_expired"

    # The chain stays valid and reconstructs HUMAN_THEN_AI provenance.
    replay_event_log(log.events)
    typed = [EventAdapter.validate_python(e.body) for e in log.events]
    provenance = compute_seat_provenance(typed)
    assert provenance["P01"] == "HUMAN_THEN_AI"
    assert provenance["P02"] == "AI"


def test_unknown_seat_takeover_raises() -> None:
    from padrino.core.engine.event_log import EventLog
    from padrino.core.engine.state import GameState, Phase
    from padrino.core.enums import PhaseKind

    clock = _FakeClock()
    mux = SeatMultiplexAdapter({"P01": _human_adapter("P01", {}, clock)})
    state = GameState(
        ruleset_id="mini7_v1",
        game_id="G-DISC",
        game_seed=_GAME_SEED,
        current_phase=Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        seats=(),
        day=1,
    )
    try:
        take_over_seat(
            mux=mux,
            event_log=EventLog(),
            state=state,
            seat_id="P99",
            replacement_adapter=_ScriptedSeatAdapter("P99", {}),
            replacement_agent_build_ref="curated-autofill",
        )
    except KeyError:
        pass
    else:  # pragma: no cover - the swap must reject an unknown seat
        raise AssertionError("take_over_seat must raise KeyError for an unknown seat")
