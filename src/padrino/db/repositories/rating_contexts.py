"""Repository helpers for first-class rating contexts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LeagueKind, RatingContextKind
from padrino.core.rulesets import get_ruleset
from padrino.db.models import League, RatingContext


@dataclass(frozen=True, slots=True)
class DeclaredRatingContext:
    """Rating-context metadata declared by a ruleset module."""

    kind: RatingContextKind
    is_canonical: bool
    display_label: str


def declared_for_ruleset(ruleset_id: str) -> DeclaredRatingContext | None:
    """Return the context declared by a known ruleset, or ``None`` fail-closed."""
    try:
        ruleset = get_ruleset(ruleset_id)
    except ValueError:
        return None

    kind = getattr(ruleset, "RATING_CONTEXT_KIND", None)
    if not isinstance(kind, RatingContextKind):
        return None
    is_canonical = bool(getattr(ruleset, "IS_CANONICAL", False))
    display_label = str(getattr(ruleset, "RATING_CONTEXT_DISPLAY_LABEL", "")).strip()
    if not display_label:
        return None
    return DeclaredRatingContext(kind=kind, is_canonical=is_canonical, display_label=display_label)


async def get_by_ruleset_kind(
    session: AsyncSession,
    *,
    ruleset_id: str,
    kind: RatingContextKind,
) -> RatingContext | None:
    """Fetch a context by its only natural key: ``(ruleset_id, kind)``."""
    stmt = select(RatingContext).where(
        RatingContext.ruleset_id == ruleset_id,
        RatingContext.kind == kind.value,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def ensure_declared_context(
    session: AsyncSession,
    *,
    ruleset_id: str,
) -> RatingContext | None:
    """Materialize a known ruleset's declared context.

    Unknown or malformed rulesets return ``None`` so callers cannot accidentally
    default into the canonical ladder.
    """
    declared = declared_for_ruleset(ruleset_id)
    if declared is None:
        return None

    existing = await get_by_ruleset_kind(session, ruleset_id=ruleset_id, kind=declared.kind)
    if existing is not None:
        return existing

    obj = RatingContext(
        kind=declared.kind.value,
        ruleset_id=ruleset_id,
        is_canonical=declared.is_canonical,
        display_label=declared.display_label,
    )
    session.add(obj)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return await get_by_ruleset_kind(session, ruleset_id=ruleset_id, kind=declared.kind)
    return obj


async def resolve_canonical_team_context(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> RatingContext | None:
    """Resolve the CANONICAL_TEAM marker for a scientific league.

    This is deliberately fail-closed: any missing league, non-scientific league,
    unknown ruleset, non-canonical declaration, missing context row, or
    non-canonical context row returns ``None`` and rating writes are skipped.
    """
    league = await session.get(League, league_id)
    if league is None:
        return None
    if league.kind != LeagueKind.SCIENTIFIC.value:
        return None
    if league.ruleset_id != ruleset_id:
        return None

    declared = declared_for_ruleset(ruleset_id)
    if declared is None:
        return None
    if declared.kind is not RatingContextKind.CANONICAL_TEAM or not declared.is_canonical:
        return None

    context = await get_by_ruleset_kind(
        session,
        ruleset_id=ruleset_id,
        kind=RatingContextKind.CANONICAL_TEAM,
    )
    if context is None:
        return None
    if context.kind != RatingContextKind.CANONICAL_TEAM.value or not context.is_canonical:
        return None
    return context


__all__ = [
    "DeclaredRatingContext",
    "declared_for_ruleset",
    "ensure_declared_context",
    "get_by_ruleset_kind",
    "resolve_canonical_team_context",
]
