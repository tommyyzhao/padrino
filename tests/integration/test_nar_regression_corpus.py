"""Regression corpus pinning pre-NAR mini7/bench10 event hash chains."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

import pytest

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import BUILTIN_RULESET_IDS, bench10_v1, get_ruleset, mini7_v1
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


def _canonical_draw_factory(ruleset_id: str) -> Callable[[], Awaitable[GameOutcome]]:
    async def _draw() -> GameOutcome:
        return await run_game(
            GameConfig(
                game_id=f"G-CORPUS-{ruleset_id.upper()}",
                game_seed=f"nar-corpus-{ruleset_id}-draw",
                ruleset_id=ruleset_id,
                timeout_s=1.0,
            ),
            NoopMockAdapter(),
            ranked=False,
        )

    return _draw


def _event_hash_chain_digest(outcome: GameOutcome) -> str:
    joined = "\n".join(stored.event_hash for stored in outcome.event_log.events)
    return hashlib.sha256(joined.encode()).hexdigest()


CANONICAL_CORPUS_CASES: tuple[
    tuple[str, str, Callable[[], Awaitable[GameOutcome]], dict[str, str | int]],
    ...,
] = (
    (
        mini7_v1.RULESET_ID,
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
        bench10_v1.RULESET_ID,
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
    (
        "roleblock10_v1",
        "roleblock10_draw",
        _canonical_draw_factory("roleblock10_v1"),
        {
            "terminal_result": "DRAW",
            "terminal_reason": "MAX_DAYS_REACHED",
            "event_count": 125,
            "final_event_hash": "ba70aebe976ad07334ac8938dbcdb1f4b33cb0b96c8fe550ac9b236d7f48c74b",
            "event_hash_chain_sha256": (
                "7c318d2a0ec1a695896f4cade1f9bd0561b1708995dc8c9b245ee23cb4a411b6"
            ),
        },
    ),
    (
        "deception13_v1",
        "deception13_draw",
        _canonical_draw_factory("deception13_v1"),
        {
            "terminal_result": "DRAW",
            "terminal_reason": "MAX_DAYS_REACHED",
            "event_count": 140,
            "final_event_hash": "e4ce115cbb0c59ecf6d585125e9a05ba39c422147c973cc570a6c7157fb68028",
            "event_hash_chain_sha256": (
                "d94daaf46114d8d135cec04e1683408110a20d6dd3cd8ad0c7a8160579c0da22"
            ),
        },
    ),
    (
        "visit12_v1",
        "visit12_draw",
        _canonical_draw_factory("visit12_v1"),
        {
            "terminal_result": "DRAW",
            "terminal_reason": "MAX_DAYS_REACHED",
            "event_count": 135,
            "final_event_hash": "5f6bc9ca6da3a5bac97b499e87126e7be27a0a10b624bcc56957bbbb38bb162b",
            "event_hash_chain_sha256": (
                "04fb79c4805507c539f10165c74f8f4ff03f3a1ea4e43c798b32efa6f5c477c8"
            ),
        },
    ),
    (
        "ninja13_v1",
        "ninja13_draw",
        _canonical_draw_factory("ninja13_v1"),
        {
            "terminal_result": "DRAW",
            "terminal_reason": "MAX_DAYS_REACHED",
            "event_count": 140,
            "final_event_hash": "b307d2be9da33f226433c0a21ce43ba682670f8037b0c038d8ddadf5492e3bfb",
            "event_hash_chain_sha256": (
                "8c1db10b96a03530b0f925088727619a9925a6540ceac1a8c999eab86320b867"
            ),
        },
    ),
)


@pytest.mark.parametrize(
    ("ruleset_id", "case", "factory", "expected"),
    CANONICAL_CORPUS_CASES,
)
async def test_nar_refactor_preserves_existing_event_hash_corpus(
    ruleset_id: str,
    case: str,
    factory: Callable[[], Awaitable[GameOutcome]],
    expected: dict[str, str | int],
) -> None:
    outcome = await factory()

    assert ruleset_id
    assert case
    assert outcome.final_state.terminal_result == expected["terminal_result"]
    assert outcome.final_state.terminal_reason == expected["terminal_reason"]
    assert len(outcome.event_log.events) == expected["event_count"]
    assert outcome.event_log.events[-1].event_hash == expected["final_event_hash"]
    assert _event_hash_chain_digest(outcome) == expected["event_hash_chain_sha256"]


def test_every_builtin_canonical_ruleset_has_a_pinned_corpus_hash() -> None:
    canonical_ids = {
        ruleset_id
        for ruleset_id in BUILTIN_RULESET_IDS
        if get_ruleset(ruleset_id).IS_CANONICAL is True
    }
    pinned_ids = {ruleset_id for ruleset_id, _case, _factory, _expected in CANONICAL_CORPUS_CASES}

    assert pinned_ids == canonical_ids
