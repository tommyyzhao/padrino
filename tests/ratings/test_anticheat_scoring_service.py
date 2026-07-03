"""US-312: ranked anti-cheat scoring runs read-side and preserves ratings."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.models import HumanRating, HumanRatingEvent, RankedFlag, RankedGameSignal
from padrino.db.repositories import games, human_principals, leagues
from padrino.ratings.anticheat_scoring_service import score_ranked_flags
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA, SCOPE_GLOBAL
from padrino.settings import Settings

_NOW = datetime(2026, 6, 27, 15, 30, tzinfo=UTC)


def _rating_snapshot(rows: Sequence[HumanRating]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row.id),
            "league_id": str(row.league_id),
            "human_player_id": row.human_player_id,
            "scope_type": row.scope_type,
            "scope_value": row.scope_value,
            "mu": row.mu,
            "sigma": row.sigma,
            "conservative_score": row.conservative_score,
            "games": row.games,
            "last_game_at": row.last_game_at,
            "quarantined_at": row.quarantined_at,
        }
        for row in sorted(rows, key=lambda item: str(item.id))
    ]


def _event_snapshot(rows: Sequence[HumanRatingEvent]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row.id),
            "league_id": str(row.league_id),
            "game_id": str(row.game_id),
            "human_player_id": row.human_player_id,
            "public_player_id": row.public_player_id,
            "scope_type": row.scope_type,
            "scope_value": row.scope_value,
            "before_mu": row.before_mu,
            "before_sigma": row.before_sigma,
            "after_mu": row.after_mu,
            "after_sigma": row.after_sigma,
        }
        for row in sorted(rows, key=lambda item: str(item.id))
    ]


async def _seed_ranked_signal_with_rating(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    source_ip_hash: str,
    game_suffix: int,
    created_at: datetime,
) -> None:
    league = await leagues.get_or_create_humans_included(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    game = await games.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"seed-anticheat-{game_suffix}",
        status="COMPLETED",
    )
    game.ranked_source_ip_hash = source_ip_hash
    session.add(
        RankedGameSignal(
            game_id=game.id,
            human_principal_id=principal_id,
            source_ip_hash=source_ip_hash,
            session_fingerprint_hash=None,
            winner="TOWN",
            faction="TOWN",
            signals={},
            created_at=created_at,
        )
    )
    session.add(
        HumanRating(
            league_id=league.id,
            human_player_id=str(principal_id),
            scope_type=SCOPE_GLOBAL,
            scope_value="global",
            mu=INITIAL_MU + game_suffix,
            sigma=INITIAL_SIGMA,
            conservative_score=(INITIAL_MU + game_suffix) - 3.0 * INITIAL_SIGMA,
            games=game_suffix,
            last_game_at=created_at,
            updated_at=created_at,
        )
    )
    session.add(
        HumanRatingEvent(
            league_id=league.id,
            game_id=game.id,
            human_player_id=str(principal_id),
            public_player_id=f"P{game_suffix:02d}",
            scope_type=SCOPE_GLOBAL,
            scope_value="global",
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU + game_suffix,
            after_sigma=INITIAL_SIGMA - 1,
            created_at=created_at,
        )
    )


@pytest.mark.asyncio
async def test_scorer_persists_flags_without_modifying_human_rating_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_ip_hash = "a" * 64
    async with session_factory() as session, session.begin():
        principals = [
            await human_principals.create_principal(
                session,
                kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
                display_name=None,
            )
            for _ in range(3)
        ]
        for index, principal in enumerate(principals, start=1):
            await _seed_ranked_signal_with_rating(
                session,
                principal_id=principal.id,
                source_ip_hash=source_ip_hash,
                game_suffix=index,
                created_at=_NOW - timedelta(minutes=index),
            )

    async with session_factory() as session:
        before_ratings = _rating_snapshot(
            (await session.execute(select(HumanRating))).scalars().all()
        )
        before_events = _event_snapshot(
            (await session.execute(select(HumanRatingEvent))).scalars().all()
        )

    strict_settings = Settings(
        _env_file=None,
        padrino_ranked_flags_source_ip_min_principals=4,
    )
    async with session_factory() as session, session.begin():
        assert await score_ranked_flags(session, settings=strict_settings, now=_NOW) == []

    permissive_settings = Settings(
        _env_file=None,
        padrino_ranked_flags_source_ip_min_principals=3,
    )
    async with session_factory() as session, session.begin():
        created = await score_ranked_flags(session, settings=permissive_settings, now=_NOW)

    assert len(created) == 3
    assert {flag.kind for flag in created} == {"same_ip_cluster"}
    assert {flag.severity for flag in created} == {"medium"}

    async with session_factory() as session:
        after_ratings = _rating_snapshot(
            (await session.execute(select(HumanRating))).scalars().all()
        )
        after_events = _event_snapshot(
            (await session.execute(select(HumanRatingEvent))).scalars().all()
        )
        flag_count = (
            await session.execute(select(func.count()).select_from(RankedFlag))
        ).scalar_one()

    assert flag_count == 3
    assert after_ratings == before_ratings
    assert after_events == before_events


def test_rating_writer_does_not_import_batch_anticheat_scorer() -> None:
    module_path = (
        Path(__file__).resolve().parents[2] / "src/padrino/ratings/human_openskill_service.py"
    )
    source = module_path.read_text()

    assert "anticheat_scoring_service" not in source
    assert "ranked_flags" not in source
