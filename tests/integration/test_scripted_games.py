"""End-to-end scripted-game integration tests for the mini7_v1 ruleset.

Three full games (Town win, Mafia win, Draw) are driven through
:func:`padrino.runner.game_runner.run_game` via :class:`DeterministicMockAdapter`.

Each test asserts the four cross-cutting invariants required by US-027:

* the terminal result matches the scripted outcome,
* alive count is non-increasing across the event stream,
* no events follow ``GameTerminated``,
* the event log's hash chain replays cleanly end-to-end.

These tests use the deterministic mock adapter only — they do NOT hit any real
LLM provider and therefore are not marked ``@pytest.mark.integration`` (the
``integration`` marker is reserved for tests that exercise live providers).
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.win_conditions import REASON_MAX_DAYS_REACHED
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import (
    make_mafia_win_script,
    make_town_win_script,
    make_villager_script,
    mini7_phase_ids,
)

_GAME_SEED = "seed-integration-001"


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


def _config() -> GameConfig:
    return GameConfig(game_id="G-INTEGRATION", game_seed=_GAME_SEED, timeout_s=1.0)


def _adapter(script: Mapping[tuple[str, str], AgentResponse]) -> DeterministicMockAdapter:
    return DeterministicMockAdapter(script)


def _assert_common_invariants(outcome: GameOutcome, expected_winner: str) -> None:
    """Apply the four US-027 invariants common to every scripted game."""
    events = outcome.event_log.events
    bodies = [stored.body for stored in events]

    assert outcome.final_state.terminal_result == expected_winner
    assert bodies[-1]["event_type"] == "GameTerminated"
    assert bodies[-1]["payload"]["winner"] == expected_winner

    terminated_idx = next(
        i for i, body in enumerate(bodies) if body["event_type"] == "GameTerminated"
    )
    assert terminated_idx == len(bodies) - 1, "no events may follow GameTerminated"

    alive_count = mini7_v1.PLAYER_COUNT
    seen_first_phase = False
    for body in bodies:
        if body["event_type"] == "PhaseStarted":
            seen_first_phase = True
        if body["event_type"] == "PlayerEliminated":
            alive_count -= 1
        assert alive_count >= 0, "alive count went negative"
    assert seen_first_phase, "at least one PhaseStarted event must be emitted"

    replayed = replay_event_log(events)
    assert len(replayed.events) == len(events)
    for original, repeated in zip(events, replayed.events, strict=True):
        assert original.event_hash == repeated.event_hash
        assert original.prev_event_hash == repeated.prev_event_hash
        assert original.sequence == repeated.sequence


async def test_scripted_town_win_eliminates_both_mafia() -> None:
    """Town wins by Day 3: D1 vote eliminates one mafia, D2 vote eliminates the other."""
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    _assert_common_invariants(outcome, expected_winner="TOWN")

    final_seats = {s.public_player_id: s for s in outcome.final_state.seats}
    assert all(not final_seats[mid].alive for mid in mafia), "both mafia must be eliminated"
    assert outcome.final_state.day <= 3, "town win must arrive by Day 3"


async def test_scripted_mafia_win_reaches_parity_by_day_four() -> None:
    """Mafia wins by Day 4: three consecutive night kills reach parity."""
    mafia, town, _, _ = _split_factions()
    script = make_mafia_win_script(mafia_ids=mafia, town_ids=town)
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    _assert_common_invariants(outcome, expected_winner="MAFIA")

    final_seats = {s.public_player_id: s for s in outcome.final_state.seats}
    alive_mafia = sum(1 for mid in mafia if final_seats[mid].alive)
    alive_town = sum(1 for tid in town if final_seats[tid].alive)
    assert alive_mafia >= alive_town, "mafia win requires parity or better"
    assert outcome.final_state.day <= 4, "mafia parity must arrive by Day 4"


async def test_scripted_draw_at_max_days_with_no_winner() -> None:
    """Draw: every seat abstains and NOOPs for the whole game, terminating at MAX_DAYS."""
    mafia, town, _, _ = _split_factions()
    seat_ids = mafia + town
    script = make_villager_script(seat_ids, mini7_phase_ids())
    outcome = await run_game(_config(), _adapter(script), ranked=False)
    _assert_common_invariants(outcome, expected_winner="DRAW")

    final_bodies = [stored.body for stored in outcome.event_log.events]
    assert final_bodies[-1]["payload"]["reason"] == REASON_MAX_DAYS_REACHED
    assert outcome.final_state.terminal_reason == REASON_MAX_DAYS_REACHED
    # All seats are still alive in a pure-abstain game.
    assert all(seat.alive for seat in outcome.final_state.seats)
