"""Deterministic gauntlet scheduler.

Given a league, ruleset, prompt version, clone count, gauntlet seed, and the
PLAYER_COUNT-sized roster of agent builds, this module inserts:

* one ``gauntlets`` row,
* ``PLAYER_COUNT`` ``gauntlet_roster_slots`` rows (one per seat), and
* ``clone_count`` ``games`` rows whose ``game_seed`` is deterministically
  derived from the gauntlet seed.

All writes happen inside a single ``session.begin()`` transaction so a failure
on any insert rolls back the whole gauntlet — no partial state ever lands in
the DB.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import RatingContextKind
from padrino.core.rulesets import get_ruleset
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.observability.events import EVENT_GAUNTLET_CREATED

_logger = structlog.get_logger("padrino.gauntlets")

MIN_CLONE_COUNT: Final[int] = 1
MAX_CLONE_COUNT: Final[int] = 100
MAX_PAIR_COUNT: Final[int] = MAX_CLONE_COUNT // 2


@dataclass(frozen=True)
class GauntletCreated:
    gauntlet_id: uuid.UUID
    game_ids: tuple[uuid.UUID, ...]


def derive_game_seed(gauntlet_seed: str, index: int) -> str:
    """Return ``sha256_hex(b'game' + gauntlet_seed + index_be4)`` for game ``index``."""
    return hashlib.sha256(b"game" + gauntlet_seed.encode() + index.to_bytes(4, "big")).hexdigest()


def derive_pair_id(gauntlet_seed: str, index: int) -> uuid.UUID:
    """Return a deterministic UUID for mirror pair ``index``."""
    digest = hashlib.sha256(
        b"pair" + gauntlet_seed.encode("utf-8") + index.to_bytes(4, "big")
    ).digest()
    return uuid.UUID(bytes=digest[:16])


def validate_placement_roster_faction_uniqueness(
    *,
    ruleset_id: str,
    gauntlet_seed: str,
    clone_count: int,
    roster: list[uuid.UUID],
) -> None:
    """Reject placement rosters that put one build in multiple factions per game."""
    declared = rating_contexts_repo.declared_for_ruleset(ruleset_id)
    if declared is None:
        return
    if declared.kind is not RatingContextKind.PLACEMENT or declared.is_canonical:
        return

    ruleset = get_ruleset(ruleset_id)
    for game_index in range(clone_count):
        game_seed = derive_game_seed(gauntlet_seed, game_index)
        faction_by_build: dict[uuid.UUID, str] = {}
        for seat in assign_roles(game_seed, ruleset):
            agent_build_id = roster[seat.seat_index]
            faction = seat.faction.value
            existing = faction_by_build.get(agent_build_id)
            if existing is None:
                faction_by_build[agent_build_id] = faction
                continue
            if existing != faction:
                raise ValueError(
                    "placement roster assigns agent_build_id "
                    f"{agent_build_id} to multiple factions in clone {game_index}: "
                    f"{existing!r} and {faction!r}"
                )


async def create_gauntlet(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    prompt_version_id: uuid.UUID,
    clone_count: int,
    gauntlet_seed: str,
    roster: list[uuid.UUID],
) -> GauntletCreated:
    """Create a gauntlet + its roster slots + ``clone_count`` child games atomically.

    ``roster`` MUST have length matching the ruleset's PLAYER_COUNT. ``clone_count``
    is clamped to ``[MIN_CLONE_COUNT, MAX_CLONE_COUNT]``. Per-game seeds are
    derived via :func:`derive_game_seed` so the same gauntlet seed always
    produces the same per-game seeds.
    """
    ruleset = get_ruleset(ruleset_id)
    if len(roster) != ruleset.PLAYER_COUNT:
        raise ValueError(
            f"roster must have exactly {ruleset.PLAYER_COUNT} entries, got {len(roster)}"
        )
    if not (MIN_CLONE_COUNT <= clone_count <= MAX_CLONE_COUNT):
        raise ValueError(
            f"clone_count must be in [{MIN_CLONE_COUNT}, {MAX_CLONE_COUNT}], got {clone_count}"
        )
    validate_placement_roster_faction_uniqueness(
        ruleset_id=ruleset_id,
        gauntlet_seed=gauntlet_seed,
        clone_count=clone_count,
        roster=roster,
    )

    # Local imports avoid module-load-order coupling between the scheduler and
    # the games repo (which would otherwise import in a different order than
    # gauntlets in tests/db).
    from padrino.db.repositories import games as games_repo

    async with session.begin():
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league_id,
            ruleset_id=ruleset_id,
            prompt_version_id=prompt_version_id,
            clone_count=clone_count,
            gauntlet_seed=gauntlet_seed,
            ranked=True,
        )
        for slot_index, agent_build_id in enumerate(roster):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, slot_index, agent_build_id)
        game_ids: list[uuid.UUID] = []
        for i in range(clone_count):
            game = await games_repo.create(
                session,
                ruleset_id=ruleset_id,
                game_seed=derive_game_seed(gauntlet_seed, i),
                gauntlet_id=gauntlet.id,
            )
            game_ids.append(game.id)

    _logger.info(
        EVENT_GAUNTLET_CREATED,
        gauntlet_id=str(gauntlet.id),
        league_id=str(league_id),
        ruleset_id=ruleset_id,
        clone_count=clone_count,
        games=[str(gid) for gid in game_ids],
    )
    return GauntletCreated(gauntlet_id=gauntlet.id, game_ids=tuple(game_ids))


async def create_paired_gauntlet(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    prompt_version_id: uuid.UUID,
    pair_count: int,
    gauntlet_seed: str,
    roster: list[uuid.UUID],
) -> GauntletCreated:
    """Create a gauntlet whose child games are deterministic mirror pairs.

    Each pair persists two distinct ``games`` rows that share one
    ``game_seed`` and ``pair_id``. ``pair_leg=0`` uses the roster's seat order;
    ``pair_leg=1`` is interpreted by the runner scheduler as the mirrored seat
    order. The stored ``clone_count`` remains the number of child game rows.
    """
    ruleset = get_ruleset(ruleset_id)
    if len(roster) != ruleset.PLAYER_COUNT:
        raise ValueError(
            f"roster must have exactly {ruleset.PLAYER_COUNT} entries, got {len(roster)}"
        )
    if not (MIN_CLONE_COUNT <= pair_count <= MAX_PAIR_COUNT):
        raise ValueError(
            f"pair_count must be in [{MIN_CLONE_COUNT}, {MAX_PAIR_COUNT}], got {pair_count}"
        )

    from padrino.db.repositories import games as games_repo

    clone_count = pair_count * 2
    async with session.begin():
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league_id,
            ruleset_id=ruleset_id,
            prompt_version_id=prompt_version_id,
            clone_count=clone_count,
            gauntlet_seed=gauntlet_seed,
            ranked=True,
        )
        for slot_index, agent_build_id in enumerate(roster):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, slot_index, agent_build_id)
        game_ids: list[uuid.UUID] = []
        for pair_index in range(pair_count):
            pair_id = derive_pair_id(gauntlet_seed, pair_index)
            game_seed = derive_game_seed(gauntlet_seed, pair_index)
            for pair_leg in (0, 1):
                game = await games_repo.create(
                    session,
                    ruleset_id=ruleset_id,
                    game_seed=game_seed,
                    gauntlet_id=gauntlet.id,
                    pair_id=pair_id,
                    pair_leg=pair_leg,
                )
                game_ids.append(game.id)

    _logger.info(
        EVENT_GAUNTLET_CREATED,
        gauntlet_id=str(gauntlet.id),
        league_id=str(league_id),
        ruleset_id=ruleset_id,
        clone_count=clone_count,
        games=[str(gid) for gid in game_ids],
    )
    return GauntletCreated(gauntlet_id=gauntlet.id, game_ids=tuple(game_ids))


__all__ = [
    "MAX_CLONE_COUNT",
    "MAX_PAIR_COUNT",
    "MIN_CLONE_COUNT",
    "GauntletCreated",
    "create_gauntlet",
    "create_paired_gauntlet",
    "derive_game_seed",
    "derive_pair_id",
    "validate_placement_roster_faction_uniqueness",
]
