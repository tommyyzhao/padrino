"""Deterministic seat permutations for multi-game tournaments (US-084).

A heterogeneous tournament plays the SAME roster across many games but rotates
which seat (and therefore which role, since roles are assigned by seat index)
each roster slot occupies. The permutation is derived from a
:class:`~padrino.core.engine.rng.SeededRng` so the same gauntlet seed always
produces the same seat orderings — pure, reproducible, no wall-clock and no
``random`` module.

``seat_permutation(seed, n)[i]`` is the roster-slot index assigned to game
seat ``i``. Because it is a full permutation of ``range(n)``, every roster
slot is used exactly once per game, so seat exposure is balanced across games.

Pure-core module: imports only :class:`SeededRng`.
"""

from __future__ import annotations

from padrino.core.engine.rng import SeededRng


def seat_permutation(seed: str, player_count: int) -> tuple[int, ...]:
    """Return a deterministic permutation of ``range(player_count)``.

    ``result[i]`` is the roster-slot index that game seat ``i`` draws from.
    Deterministic in ``seed``: the same seed yields the same permutation.
    """
    if player_count <= 0:
        raise ValueError("player_count must be > 0")
    return tuple(SeededRng(seed).shuffle(list(range(player_count))))


__all__ = ["seat_permutation"]
