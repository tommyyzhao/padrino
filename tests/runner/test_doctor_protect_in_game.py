"""Doctor protect through the real runner loop (regression for the self-repeat bug).

``ProtectSubmitted`` is folded into state before the night resolves; the night must
still be resolved against the pre-submission ``last_protected_target``.
"""

from __future__ import annotations

from typing import Any

from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import ScriptedAction, make_role_aware_script, mini7_phase_ids

_SEED = "seed-doctor-protect-001"


async def _play(protects: dict[int, int], kills: dict[int, int]) -> tuple[GameOutcome, list[str]]:
    """``protects``/``kills`` map night -> index into the non-doctor town seats."""
    seats = assign_roles(_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    others = [
        s.public_player_id for s in seats if s.faction is Faction.TOWN and s.role is not Role.DOCTOR
    ]
    actions: dict[tuple[str, str], ScriptedAction] = {}
    for night, idx in kills.items():
        for m in mafia:
            actions[(f"NIGHT_{night}_ACTIONS", m)] = ScriptedAction(
                ActionType.MAFIA_KILL, others[idx]
            )
    for night, idx in protects.items():
        actions[(f"NIGHT_{night}_ACTIONS", doctor)] = ScriptedAction(
            ActionType.PROTECT, others[idx]
        )
    script = make_role_aware_script(
        [s.public_player_id for s in seats], mini7_phase_ids(), actions=actions
    )
    outcome = await run_game(
        GameConfig(game_id="G-DOC", game_seed=_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
    )
    replay_event_log(list(outcome.event_log.events))  # hash chain still verifies
    return outcome, others


def _nights(outcome: GameOutcome) -> list[dict[str, Any]]:
    return [
        s.body["payload"]
        for s in outcome.event_log.events
        if s.body["event_type"] == "NightResolved"
    ]


async def test_night1_protect_of_kill_target_saves_them() -> None:
    outcome, others = await _play({1: 0}, {1: 0})
    n1 = _nights(outcome)[0]
    assert n1["protected"] == others[0]
    assert n1["mafia_kill_target"] == others[0]
    assert n1["eliminated"] is None
    seat = {s.public_player_id: s for s in outcome.final_state.seats}[others[0]]
    assert seat.alive


async def test_night2_repeat_protect_is_rejected() -> None:
    outcome, others = await _play({1: 0, 2: 0}, {1: 0, 2: 0})
    n1, n2 = _nights(outcome)[:2]
    assert n1["protected"] == others[0] and n1["eliminated"] is None
    assert n2["protected"] is None
    assert n2["eliminated"] == others[0]


async def test_night2_non_repeat_protect_works() -> None:
    outcome, others = await _play({1: 0, 2: 1}, {1: 0, 2: 1})
    n1, n2 = _nights(outcome)[:2]
    assert n1["protected"] == others[0] and n1["eliminated"] is None
    assert n2["protected"] == others[1] and n2["eliminated"] is None
