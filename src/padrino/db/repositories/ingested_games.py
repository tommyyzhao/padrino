"""CRUD helpers for :class:`padrino.db.models.IngestedGame` (US-062).

Ingested rows are kept in a dedicated table so externally-submitted games do
not commingle with locally-run rows in ``games``. ``game_id`` is the unique
key — re-submission of the same bundle is idempotent.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import IngestedGame

VERIFIED: str = "verified"
UNVERIFIED: str = "unverified"


async def create(
    session: AsyncSession,
    *,
    game_id: str,
    ruleset_id: str,
    league_id: str | None,
    gauntlet_id: str | None,
    tip_hash: str,
    signer_fingerprint: str | None,
    verification_status: str,
    submitter_key_id: uuid.UUID | None,
    bundle: dict[str, Any],
) -> IngestedGame:
    obj = IngestedGame(
        game_id=game_id,
        ruleset_id=ruleset_id,
        league_id=league_id,
        gauntlet_id=gauntlet_id,
        tip_hash=tip_hash,
        signer_fingerprint=signer_fingerprint,
        verification_status=verification_status,
        submitter_key_id=submitter_key_id,
        bundle=dict(bundle),
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_by_game_id(session: AsyncSession, game_id: str) -> IngestedGame | None:
    stmt = select(IngestedGame).where(IngestedGame.game_id == game_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def unverified_game_ids(session: AsyncSession) -> set[str]:
    """Return the ``game_id`` of every ingested row not centrally verified.

    Used by the public rating/analytics surfaces to exclude self-signed
    ingested bundles that were never verified — a submitter-scoped key must
    not be able to pollute the public rankings (US-112). ``game_id`` is the
    string form of the original :class:`padrino.db.models.Game` UUID, so the
    returned set can be diffed directly against ``str(game.id)``.
    """
    stmt = select(IngestedGame.game_id).where(IngestedGame.verification_status != VERIFIED)
    return {gid for (gid,) in (await session.execute(stmt)).all()}


async def count_by_submitter(session: AsyncSession) -> dict[uuid.UUID | None, int]:
    """Return ``{submitter_key_id: count}`` across every ingested row.

    The ``None`` bucket counts admin-submitted bundles (no submitter api_key id).
    """
    from sqlalchemy import func

    stmt = select(IngestedGame.submitter_key_id, func.count(IngestedGame.id)).group_by(
        IngestedGame.submitter_key_id
    )
    out: dict[uuid.UUID | None, int] = {}
    for sid, count in (await session.execute(stmt)).all():
        out[sid] = int(count)
    return out


__all__ = [
    "UNVERIFIED",
    "VERIFIED",
    "count_by_submitter",
    "create",
    "get_by_game_id",
    "unverified_game_ids",
]
