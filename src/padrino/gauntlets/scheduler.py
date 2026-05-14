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

from padrino.core.rulesets import mini7_v1
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.observability.events import EVENT_GAUNTLET_CREATED

_logger = structlog.get_logger("padrino.gauntlets")

MIN_CLONE_COUNT: Final[int] = 1
MAX_CLONE_COUNT: Final[int] = 100


@dataclass(frozen=True)
class GauntletCreated:
    gauntlet_id: uuid.UUID
    game_ids: tuple[uuid.UUID, ...]


def derive_game_seed(gauntlet_seed: str, index: int) -> str:
    """Return ``sha256_hex(b'game' + gauntlet_seed + index_be4)`` for game ``index``."""
    return hashlib.sha256(b"game" + gauntlet_seed.encode() + index.to_bytes(4, "big")).hexdigest()


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

    ``roster`` MUST have length ``mini7_v1.PLAYER_COUNT`` (=7). ``clone_count``
    is clamped to ``[MIN_CLONE_COUNT, MAX_CLONE_COUNT]``. Per-game seeds are
    derived via :func:`derive_game_seed` so the same gauntlet seed always
    produces the same per-game seeds.
    """
    if len(roster) != mini7_v1.PLAYER_COUNT:
        raise ValueError(
            f"roster must have exactly {mini7_v1.PLAYER_COUNT} entries, got {len(roster)}"
        )
    if not (MIN_CLONE_COUNT <= clone_count <= MAX_CLONE_COUNT):
        raise ValueError(
            f"clone_count must be in [{MIN_CLONE_COUNT}, {MAX_CLONE_COUNT}], got {clone_count}"
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


__all__ = ["MAX_CLONE_COUNT", "MIN_CLONE_COUNT", "GauntletCreated", "create_gauntlet"]
