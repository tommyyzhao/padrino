"""Regression tests for shared human seat authorization helpers."""

from __future__ import annotations

from padrino.api import human_actions, human_chat, human_observation, human_turing
from padrino.api.human_seat_auth import resolve_human_game_seat


def test_human_routes_share_one_seat_authorization_helper() -> None:
    """Action/chat/observation/turing use the same wrong-seat resolver."""
    consumers = (human_actions, human_chat, human_observation, human_turing)

    for module in consumers:
        assert module.resolve_human_game_seat is resolve_human_game_seat
        assert "_resolve_seat" not in module.__dict__
