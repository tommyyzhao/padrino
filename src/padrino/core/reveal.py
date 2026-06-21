"""Canonical endgame reveal projection (Wave 9, US-143).

At the end of a game every player always sees the full truth (decision 11):
which seats were human, each seat's role / faction, the exact model for AI
seats, and any silent AI takeover provenance. The review found FIVE divergent
ad-hoc reveal shapes across the human-multiplayer epics; this module is the ONE
canonical reveal schema so every surface (API, frontend, exports) agrees.

The projection is pure (data-in / no IO): the impure shell resolves seat rows,
event-log provenance, and model identity, then hands them here for assembly.
The reveal is never produced for a LIVE game — terminal gating lives in the
caller (``GET /public/games/{id}/reveal``); this module simply assembles the
truth it is given.

Takeover provenance maps directly from the seat's :class:`SeatKind`:

* ``AI``          -> ``"AI"``            (an AI seat that was never human)
* ``HUMAN``       -> ``"HUMAN"``         (a human seat held to the end)
* ``AI_TAKEOVER`` -> ``"HUMAN_THEN_AI"`` (a human seat silently taken over)

A missing / unknown seat kind fails closed to ``"AI"`` (never invents a human).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from padrino.core.enums import SeatKind

#: Takeover-provenance labels in the canonical reveal schema.
PROVENANCE_AI = "AI"
PROVENANCE_HUMAN = "HUMAN"
PROVENANCE_HUMAN_THEN_AI = "HUMAN_THEN_AI"

#: Seat kind -> provenance label. ``AI_TAKEOVER`` is a seat that *started* human
#: and was silently taken over, so its provenance is ``HUMAN_THEN_AI``.
_PROVENANCE_BY_KIND: dict[SeatKind, str] = {
    SeatKind.AI: PROVENANCE_AI,
    SeatKind.HUMAN: PROVENANCE_HUMAN,
    SeatKind.AI_TAKEOVER: PROVENANCE_HUMAN_THEN_AI,
}


class RevealModel(BaseModel):
    """Exact model identity for an AI (or taken-over) seat."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model_name: str
    model_version: str | None = None
    agent_build_id: str
    display_name: str | None = None


class SeatReveal(BaseModel):
    """The full per-seat truth disclosed at the endgame reveal.

    ``is_human`` reflects who *finished* the game in the seat: a human seat that
    was silently taken over reads ``is_human=False`` (an AI finished it) while
    ``takeover_provenance`` preserves the ``HUMAN_THEN_AI`` history. ``model`` is
    populated for any seat an AI occupied at the end (``AI`` or ``AI_TAKEOVER``)
    and is ``None`` for a seat a human held to the end.
    """

    model_config = ConfigDict(frozen=True)

    public_player_id: str
    seat_index: int
    is_human: bool
    role: str
    faction: str
    alive: bool
    takeover_provenance: str
    taken_over_at_phase: str | None = None
    model: RevealModel | None = None


class EndgameReveal(BaseModel):
    """The canonical endgame reveal: every seat's full truth."""

    model_config = ConfigDict(frozen=True)

    game_id: str
    ruleset_id: str
    winner: str | None = None
    seats: tuple[SeatReveal, ...]


class SeatRevealInput(BaseModel):
    """Impure-shell-resolved data for one seat, fed to the pure assembler.

    The shell reads these from the ``GameSeat`` row and (for AI seats) the
    ``AgentBuild`` -> ``ModelConfig`` -> ``ModelProvider`` join; the pure
    projection never touches the database.
    """

    model_config = ConfigDict(frozen=True)

    public_player_id: str
    seat_index: int
    seat_kind: SeatKind | str | None
    role: str
    faction: str
    alive: bool
    taken_over_at_phase: str | None = None
    model: RevealModel | None = None


def _coerce_kind(raw: SeatKind | str | None) -> SeatKind:
    """Resolve a seat kind, fail-closed to ``AI`` (never invents a human)."""
    if isinstance(raw, SeatKind):
        return raw
    if raw is None:
        return SeatKind.AI
    try:
        return SeatKind(str(raw))
    except ValueError:
        return SeatKind.AI


def project_seat_reveal(seat: SeatRevealInput) -> SeatReveal:
    """Assemble one seat's reveal from shell-resolved input (pure).

    ``is_human`` is true only for a seat a human held to the end (``HUMAN``); a
    taken-over seat (``AI_TAKEOVER``) reads ``is_human=False`` but keeps its
    ``HUMAN_THEN_AI`` provenance. A model is dropped for a human-held seat even
    if one was mistakenly supplied, so a human seat can never leak a model.
    """
    kind = _coerce_kind(seat.seat_kind)
    provenance = _PROVENANCE_BY_KIND[kind]
    is_human = kind is SeatKind.HUMAN
    model = None if is_human else seat.model
    return SeatReveal(
        public_player_id=seat.public_player_id,
        seat_index=seat.seat_index,
        is_human=is_human,
        role=seat.role,
        faction=seat.faction,
        alive=seat.alive,
        takeover_provenance=provenance,
        taken_over_at_phase=seat.taken_over_at_phase,
        model=model,
    )


def project_endgame_reveal(
    *,
    game_id: str,
    ruleset_id: str,
    winner: str | None,
    seats: Iterable[SeatRevealInput],
) -> EndgameReveal:
    """Assemble the canonical endgame reveal from shell-resolved seat inputs.

    Seats are emitted ordered by ``seat_index`` so the reveal is deterministic
    regardless of the input iteration order.
    """
    projected = sorted(
        (project_seat_reveal(s) for s in seats),
        key=lambda s: s.seat_index,
    )
    return EndgameReveal(
        game_id=game_id,
        ruleset_id=ruleset_id,
        winner=winner,
        seats=tuple(projected),
    )


__all__ = [
    "PROVENANCE_AI",
    "PROVENANCE_HUMAN",
    "PROVENANCE_HUMAN_THEN_AI",
    "EndgameReveal",
    "RevealModel",
    "SeatReveal",
    "SeatRevealInput",
    "project_endgame_reveal",
    "project_seat_reveal",
]
