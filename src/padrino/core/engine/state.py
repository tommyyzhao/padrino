"""Frozen Pydantic state models for the deterministic engine.

`Seat`, `Phase`, and `GameState` are immutable snapshots that resolvers operate
on. Mutation must produce a new `GameState` via `model_copy(update=...)` — never
in-place edits.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from padrino.core.enums import Faction, PhaseKind, Role


class QueuedInspection(BaseModel):
    """Detective investigation result queued for next-day delivery."""

    model_config = ConfigDict(frozen=True)

    target: str
    finding: Literal["MAFIA", "TOWN"]


class Seat(BaseModel):
    """Per-player game state. Immutable."""

    model_config = ConfigDict(frozen=True)

    public_player_id: str
    seat_index: int
    role: Role
    faction: Faction
    alive: bool
    death_phase: str | None = None
    last_protected_target: str | None = None
    queued_inspection_result: QueuedInspection | None = None


class Phase(BaseModel):
    """Phase identifier within a game. Immutable."""

    model_config = ConfigDict(frozen=True)

    kind: PhaseKind
    day: int
    round: int


class GameState(BaseModel):
    """Complete game snapshot. Immutable."""

    model_config = ConfigDict(frozen=True)

    ruleset_id: str
    game_id: str
    game_seed: str
    current_phase: Phase
    seats: tuple[Seat, ...]
    day: int
    terminal_result: str | None = None
    terminal_reason: str | None = None

    def living_seats(self) -> list[Seat]:
        """Return every seat with `alive=True`, preserving seat order."""
        return [s for s in self.seats if s.alive]

    def living_seats_by_faction(self, faction: Faction) -> list[Seat]:
        """Return living seats belonging to `faction`."""
        return [s for s in self.seats if s.alive and s.faction == faction]

    def seat_by_public_id(self, public_player_id: str) -> Seat | None:
        """Return the seat with the given public id, or None if absent."""
        for s in self.seats:
            if s.public_player_id == public_player_id:
                return s
        return None

    def alive_count_by_faction(self, faction: Faction) -> int:
        """Return the number of living seats in `faction`."""
        return sum(1 for s in self.seats if s.alive and s.faction == faction)
