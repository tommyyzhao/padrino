"""US-301: pure ranked human-rating rollback recompute."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal

import pytest

from padrino.core.anticheat.rating_rollback import (
    RatingRollbackGame,
    RatingRollbackPlayer,
    recompute_human_rating,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "padrino"
    / "core"
    / "anticheat"
    / "rating_rollback.py"
)


def _game(game_id: str, winner: Literal["TOWN", "MAFIA", "DRAW"]) -> RatingRollbackGame:
    return RatingRollbackGame(
        game_id=game_id,
        winner=winner,
        players=(
            RatingRollbackPlayer(
                public_player_id="P01",
                faction="TOWN",
                human_player_id="human-a",
            ),
            RatingRollbackPlayer(public_player_id="P02", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P03", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P04", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P05", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P06", faction="MAFIA"),
            RatingRollbackPlayer(public_player_id="P07", faction="MAFIA"),
        ),
    )


def test_recompute_excluding_middle_game_matches_fresh_sequence() -> None:
    history = (_game("game-1", "TOWN"), _game("game-2", "MAFIA"), _game("game-3", "TOWN"))

    rolled_back = recompute_human_rating(
        history,
        human_player_id="human-a",
        excluded_game_ids={"game-2"},
    )
    fresh_without_game_2 = recompute_human_rating(
        (history[0], history[2]),
        human_player_id="human-a",
        excluded_game_ids=set(),
    )

    assert rolled_back.games == 2
    assert rolled_back.mu == pytest.approx(fresh_without_game_2.mu)
    assert rolled_back.sigma == pytest.approx(fresh_without_game_2.sigma)
    assert rolled_back.conservative_score == pytest.approx(fresh_without_game_2.conservative_score)


def test_ai_opponents_are_reconstructed_as_transient_initial_ratings() -> None:
    solo_human_result = recompute_human_rating(
        (_game("game-1", "TOWN"),),
        human_player_id="human-a",
        excluded_game_ids=set(),
    )

    with_historical_human_opponent = RatingRollbackGame(
        game_id="game-1",
        winner="TOWN",
        players=(
            RatingRollbackPlayer(
                public_player_id="P01",
                faction="TOWN",
                human_player_id="human-a",
            ),
            RatingRollbackPlayer(
                public_player_id="P02",
                faction="TOWN",
                human_player_id="other-human",
                before_mu=60.0,
                before_sigma=1.0,
            ),
            RatingRollbackPlayer(public_player_id="P03", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P04", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P05", faction="TOWN"),
            RatingRollbackPlayer(public_player_id="P06", faction="MAFIA"),
            RatingRollbackPlayer(public_player_id="P07", faction="MAFIA"),
        ),
    )
    human_opponent_result = recompute_human_rating(
        (with_historical_human_opponent,),
        human_player_id="human-a",
        excluded_game_ids=set(),
    )

    assert solo_human_result.games == 1
    assert human_opponent_result.games == 1
    assert solo_human_result.mu != pytest.approx(human_opponent_result.mu)


def test_rating_rollback_module_imports_no_random_clock_or_db() -> None:
    source = MODULE_PATH.read_text()
    assert "datetime.now" not in source
    assert "datetime.utcnow" not in source
    tree = ast.parse(source)
    forbidden = {"random", "secrets", "sqlalchemy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".", 1)[0] not in forbidden
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".", 1)[0] not in forbidden
