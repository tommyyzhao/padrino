"""Regression corpus pinning pre-NAR mini7/bench10 event hash chains."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

import pytest

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import bench10_v1, mini7_v1
from padrino.llm.mock import DeterministicMockAdapter, NoopMockAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, run_game
from tests.conftest import make_town_win_script


async def _mini7_town_win() -> GameOutcome:
    seed = "nar-corpus-mini7-town"
    seats = assign_roles(seed, mini7_v1)
    mafia_ids = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town_ids = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor_id = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective_id = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    script = make_town_win_script(
        mafia_ids=mafia_ids,
        town_ids=town_ids,
        doctor_id=doctor_id,
        detective_id=detective_id,
    )
    return await run_game(
        GameConfig(
            game_id="G-NAR-MINI7-TOWN",
            game_seed=seed,
            ruleset_id=mini7_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(script),
        ranked=False,
    )


async def _bench10_draw() -> GameOutcome:
    seed = "nar-corpus-bench10-draw"
    return await run_game(
        GameConfig(
            game_id="G-NAR-BENCH10-DRAW",
            game_seed=seed,
            ruleset_id=bench10_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        NoopMockAdapter(),
        ranked=False,
    )


def _event_hash_chain_digest(outcome: GameOutcome) -> str:
    joined = "\n".join(stored.event_hash for stored in outcome.event_log.events)
    return hashlib.sha256(joined.encode()).hexdigest()


@pytest.mark.parametrize(
    ("case", "factory", "expected"),
    [
        (
            "mini7_town",
            _mini7_town_win,
            {
                "terminal_result": "TOWN",
                "terminal_reason": "ALL_MAFIA_ELIMINATED",
                "event_count": 47,
                "final_event_hash": "4333c53b496bde926c457944520dbf1c713f6711489928fb63ddd40a091701bb",
                "event_hash_chain_sha256": (
                    "df7d38afe39c6867736bb872b07765f8ff7d1f25eaed3053fdfaafbfc4d4d616"
                ),
            },
        ),
        (
            "bench10_draw",
            _bench10_draw,
            {
                "terminal_result": "DRAW",
                "terminal_reason": "MAX_DAYS_REACHED",
                "event_count": 125,
                "final_event_hash": "c241612e044bbfa75e346e61f7da3d6e31176f95ac95d32eb92d971770b0aa5f",
                "event_hash_chain_sha256": (
                    "918b471f1048def56fb8ee4d6333ae00e0fb72364c2768d8294cacc76343ac71"
                ),
            },
        ),
    ],
)
async def test_nar_refactor_preserves_existing_event_hash_corpus(
    case: str,
    factory: Callable[[], Awaitable[GameOutcome]],
    expected: dict[str, str | int],
) -> None:
    outcome = await factory()

    assert case
    assert outcome.final_state.terminal_result == expected["terminal_result"]
    assert outcome.final_state.terminal_reason == expected["terminal_reason"]
    assert len(outcome.event_log.events) == expected["event_count"]
    assert outcome.event_log.events[-1].event_hash == expected["final_event_hash"]
    assert _event_hash_chain_digest(outcome) == expected["event_hash_chain_sha256"]
