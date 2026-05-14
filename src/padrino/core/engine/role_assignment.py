"""Deterministic role assignment for a seeded game.

`assign_roles(game_seed, ruleset)` returns the canonical seven-seat roster for a
mini7-style ruleset. Role placement is driven by a `SeededRng` over the role
multiset so the same `game_seed` always yields the same assignment across
machines and Python versions.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from padrino.core.engine.rng import SeededRng
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, Role


class Ruleset(Protocol):
    """Structural ruleset interface required for role assignment."""

    PLAYER_COUNT: int
    ROLE_COUNTS: dict[Role, int]
    ROLE_FACTIONS: dict[Role, Faction]


def assign_roles(game_seed: str, ruleset: Ruleset) -> list[Seat]:
    """Return `PLAYER_COUNT` seats with deterministic role placement.

    The role multiset is expanded from `ruleset.ROLE_COUNTS`, shuffled with a
    `SeededRng` seeded by `sha256(b"roles" + game_seed)`, and zipped onto seats
    P01..P0N in order.
    """
    role_multiset: list[Role] = []
    for role, count in ruleset.ROLE_COUNTS.items():
        role_multiset.extend([role] * count)

    if len(role_multiset) != ruleset.PLAYER_COUNT:
        raise ValueError(
            f"ROLE_COUNTS sum {len(role_multiset)} != PLAYER_COUNT {ruleset.PLAYER_COUNT}"
        )

    role_seed = hashlib.sha256(b"roles" + game_seed.encode("utf-8")).digest()
    rng = SeededRng(role_seed)
    shuffled = rng.shuffle(role_multiset)

    return [
        Seat(
            public_player_id=f"P{i + 1:02d}",
            seat_index=i,
            role=role,
            faction=ruleset.ROLE_FACTIONS[role],
            alive=True,
        )
        for i, role in enumerate(shuffled)
    ]
