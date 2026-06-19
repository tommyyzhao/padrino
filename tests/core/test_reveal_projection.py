"""US-143: the canonical pure endgame reveal projection.

These cover the pure assembler in :mod:`padrino.core.reveal` directly (no IO):
takeover-provenance derivation, human seats dropping any model, seat ordering,
and the fail-closed seat-kind coercion.
"""

from __future__ import annotations

from padrino.core.enums import SeatKind
from padrino.core.reveal import (
    PROVENANCE_AI,
    PROVENANCE_HUMAN,
    PROVENANCE_HUMAN_THEN_AI,
    RevealModel,
    SeatRevealInput,
    project_endgame_reveal,
    project_seat_reveal,
)


def _model(name: str = "mock/glm") -> RevealModel:
    return RevealModel(
        provider="cerebras",
        model_name=name,
        model_version="2024-01",
        agent_build_id="11111111-1111-1111-1111-111111111111",
        display_name="GLM",
    )


def test_human_seat_is_human_and_drops_model() -> None:
    seat = SeatRevealInput(
        public_player_id="P00",
        seat_index=0,
        seat_kind=SeatKind.HUMAN,
        role="VILLAGER",
        faction="TOWN",
        alive=True,
        model=_model(),  # a stray model must never leak on a human-held seat
    )
    out = project_seat_reveal(seat)
    assert out.is_human is True
    assert out.takeover_provenance == PROVENANCE_HUMAN
    assert out.model is None


def test_ai_seat_carries_model_and_ai_provenance() -> None:
    seat = SeatRevealInput(
        public_player_id="P01",
        seat_index=1,
        seat_kind=SeatKind.AI,
        role="MAFIA_GOON",
        faction="MAFIA",
        alive=False,
        model=_model("mock/deepseek"),
    )
    out = project_seat_reveal(seat)
    assert out.is_human is False
    assert out.takeover_provenance == PROVENANCE_AI
    assert out.model is not None
    assert out.model.model_name == "mock/deepseek"


def test_takeover_seat_is_human_then_ai() -> None:
    seat = SeatRevealInput(
        public_player_id="P02",
        seat_index=2,
        seat_kind=SeatKind.AI_TAKEOVER,
        role="DOCTOR",
        faction="TOWN",
        alive=True,
        taken_over_at_phase="DAY_VOTE:2",
        model=_model(),
    )
    out = project_seat_reveal(seat)
    # An AI finished the seat, so is_human is False, but provenance preserves the
    # human-then-AI history and the takeover phase.
    assert out.is_human is False
    assert out.takeover_provenance == PROVENANCE_HUMAN_THEN_AI
    assert out.taken_over_at_phase == "DAY_VOTE:2"
    assert out.model is not None


def test_missing_seat_kind_fails_closed_to_ai() -> None:
    seat = SeatRevealInput(
        public_player_id="P03",
        seat_index=3,
        seat_kind=None,
        role="VILLAGER",
        faction="TOWN",
        alive=True,
    )
    out = project_seat_reveal(seat)
    assert out.is_human is False
    assert out.takeover_provenance == PROVENANCE_AI


def test_unknown_seat_kind_string_fails_closed_to_ai() -> None:
    seat = SeatRevealInput(
        public_player_id="P04",
        seat_index=4,
        seat_kind="GREMLIN",
        role="VILLAGER",
        faction="TOWN",
        alive=True,
    )
    out = project_seat_reveal(seat)
    assert out.is_human is False
    assert out.takeover_provenance == PROVENANCE_AI


def test_string_seat_kind_human_is_recognized() -> None:
    seat = SeatRevealInput(
        public_player_id="P05",
        seat_index=5,
        seat_kind="HUMAN",
        role="VILLAGER",
        faction="TOWN",
        alive=True,
    )
    out = project_seat_reveal(seat)
    assert out.is_human is True
    assert out.takeover_provenance == PROVENANCE_HUMAN


def test_endgame_reveal_orders_seats_by_index() -> None:
    seats = [
        SeatRevealInput(
            public_player_id="P02",
            seat_index=2,
            seat_kind=SeatKind.AI,
            role="VILLAGER",
            faction="TOWN",
            alive=True,
        ),
        SeatRevealInput(
            public_player_id="P00",
            seat_index=0,
            seat_kind=SeatKind.HUMAN,
            role="DETECTIVE",
            faction="TOWN",
            alive=True,
        ),
        SeatRevealInput(
            public_player_id="P01",
            seat_index=1,
            seat_kind=SeatKind.AI_TAKEOVER,
            role="MAFIA_GOON",
            faction="MAFIA",
            alive=False,
            taken_over_at_phase="NIGHT:1",
        ),
    ]
    reveal = project_endgame_reveal(
        game_id="g1",
        ruleset_id="mini7_v1",
        winner="TOWN",
        seats=seats,
    )
    assert [s.seat_index for s in reveal.seats] == [0, 1, 2]
    assert reveal.winner == "TOWN"
    assert reveal.game_id == "g1"
    # Full per-seat truth is present: human/AI marker, role, faction, provenance.
    by_id = {s.public_player_id: s for s in reveal.seats}
    assert by_id["P00"].is_human is True
    assert by_id["P00"].role == "DETECTIVE"
    assert by_id["P01"].takeover_provenance == PROVENANCE_HUMAN_THEN_AI
    assert by_id["P02"].is_human is False
