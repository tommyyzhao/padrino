"""Deterministic curated auto-fill for empty lobby seats (US-149).

When a private lobby launches, every AI seat that the host did not pre-pick a
model for must be filled from a curated pool of human-eligible ``AgentBuild``
ids. The assignment is PURE and deterministic: the same ``lobby_seed`` + reserved
map + curated roster always yields the same seat -> build mapping, so a launch is
reproducible and replay-stable.

This module is pure core: it imports only
:class:`~padrino.core.engine.rng.SeededRng` — no ``random``, no clock, no IO, no
DB. The impure launch handoff (``padrino.api.routes.lobbies``) reads the lobby
seat layout and the curated roster from the DB, calls this function, then
materializes the game.

The curated roster is provided as opaque string ids (an ``AgentBuild`` id's
``str``). The caller maps them back to whatever it needs; this module never
constructs domain objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from padrino.core.engine.rng import SeededRng


class NotEnoughCuratedModelsError(ValueError):
    """Raised when the curated pool is smaller than the number of empty seats.

    Auto-fill assigns each empty seat a DISTINCT curated build (no duplicate
    model in one game), so a pool smaller than the empty-seat count cannot
    satisfy the launch and the lobby must not start with a half-filled table.
    """

    def __init__(self, *, empty_seats: int, pool_size: int) -> None:
        self.empty_seats = empty_seats
        self.pool_size = pool_size
        super().__init__(
            f"curated pool of {pool_size} model(s) is smaller than {empty_seats} empty seat(s)"
        )


class AutoFillAssignment(dict[int, str]):
    """Mapping of empty ``seat_index`` -> curated ``AgentBuild`` id (as ``str``).

    A thin ``dict`` subclass purely for a self-documenting return type; it
    carries only the seats this call filled (never the reserved/human seats).
    """


def autofill_empty_seats(
    *,
    lobby_seed: str,
    empty_seat_indices: Sequence[int],
    reserved_build_ids: Mapping[int, str],
    curated_roster: Sequence[str],
) -> AutoFillAssignment:
    """Deterministically assign curated builds to the lobby's empty AI seats.

    Args:
        lobby_seed: The lobby's deterministic seed (drives the SeededRng).
        empty_seat_indices: Seat indices needing a curated model (AI seats with
            no pre-picked build). Order does not affect determinism (the result
            is sorted by seat index internally).
        reserved_build_ids: Already-assigned ``seat_index -> build id`` (host
            HUMAN seats are absent; AI seats the host pre-picked are present).
            These builds are EXCLUDED from the curated draw so the same model is
            never seated twice in one game.
        curated_roster: The curated human-eligible build-id pool (as ``str``).

    Returns:
        An :class:`AutoFillAssignment` mapping each empty seat index to a curated
        build id. Distinct seats receive distinct builds.

    Raises:
        NotEnoughCuratedModelsError: When the available pool (curated roster
            minus already-reserved builds) is smaller than the empty-seat count.

    Pure and deterministic: identical inputs always yield an identical mapping.
    """
    seats = sorted(set(empty_seat_indices))
    reserved = set(reserved_build_ids.values())

    # Dedupe the curated roster preserving its given order, then drop any build
    # already reserved on another seat (no duplicate model in one game).
    available: list[str] = []
    seen: set[str] = set()
    for build_id in curated_roster:
        if build_id in seen or build_id in reserved:
            continue
        seen.add(build_id)
        available.append(build_id)

    if len(available) < len(seats):
        raise NotEnoughCuratedModelsError(empty_seats=len(seats), pool_size=len(available))

    rng = SeededRng(f"autofill:{lobby_seed}")
    shuffled = rng.shuffle(available)

    assignment = AutoFillAssignment()
    for seat_index, build_id in zip(seats, shuffled, strict=False):
        assignment[seat_index] = build_id
    return assignment


__all__ = [
    "AutoFillAssignment",
    "NotEnoughCuratedModelsError",
    "autofill_empty_seats",
]
