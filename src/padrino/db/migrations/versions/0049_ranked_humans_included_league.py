"""Split Humans-Included league uniqueness by ranked flag (US-234a).

Wave 9 shipped a single dormant Humans-Included league per ruleset. Wave 11
needs both casual and ranked human lobbies for the same ruleset, still segregated
from the scientific benchmark. This migration replaces the partial unique index
with one keyed by ``(ruleset_id, ranked)`` for ``kind = 'HUMANS_INCLUDED'``.

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_league_humans_included_ruleset"
_WHERE = sa.text("kind = 'HUMANS_INCLUDED'")

_DEPENDENT_LEAGUE_FKS: tuple[tuple[str, str], ...] = (
    ("gauntlets", "league_id"),
    ("ratings", "league_id"),
    ("rating_events", "league_id"),
    ("human_rating", "league_id"),
    ("human_rating_event", "league_id"),
    ("lobbies", "league_id"),
)

_LEAGUE_SCOPED_UNIQUE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ratings", ("agent_build_id", "scope_type", "scope_value")),
    ("human_rating", ("human_player_id", "scope_type", "scope_value")),
)


def upgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="leagues")
    op.create_index(
        _INDEX_NAME,
        "leagues",
        ["ruleset_id", "ranked"],
        unique=True,
        sqlite_where=_WHERE,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index(_INDEX_NAME, table_name="leagues")
    _dedup_humans_included_leagues_by_ruleset(bind)
    op.create_index(
        _INDEX_NAME,
        "leagues",
        ["ruleset_id"],
        unique=True,
        sqlite_where=_WHERE,
        postgresql_where=_WHERE,
    )


def _dedup_humans_included_leagues_by_ruleset(bind: sa.engine.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, ruleset_id, ranked, created_at FROM leagues WHERE kind = 'HUMANS_INCLUDED'"
        )
    ).all()

    by_ruleset: dict[str, list[sa.engine.Row[Any]]] = {}
    for row in rows:
        by_ruleset.setdefault(row.ruleset_id, []).append(row)

    for group in by_ruleset.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda r: (
                # Prefer the old casual row on downgrade, then preserve 0045's
                # stable earliest-row keeper rule.
                bool(r.ranked),
                str(r.created_at),
                str(r.id),
            ),
        )
        keeper = ordered[0]
        losers = [r.id for r in ordered[1:]]
        for loser_id in losers:
            _delete_loser_scope_collisions(bind, keeper_id=keeper.id, loser_id=loser_id)
            for table, column in _DEPENDENT_LEAGUE_FKS:
                if not _table_exists(bind, table):
                    continue
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :keeper WHERE {column} = :loser"),
                    {"keeper": keeper.id, "loser": loser_id},
                )
            bind.execute(sa.text("DELETE FROM leagues WHERE id = :loser"), {"loser": loser_id})


def _delete_loser_scope_collisions(
    bind: sa.engine.Connection, *, keeper_id: object, loser_id: object
) -> None:
    for table, scope_columns in _LEAGUE_SCOPED_UNIQUE_TABLES:
        if not _table_exists(bind, table):
            continue
        scope_match = " AND ".join(
            f"keeper_rows.{column} = {table}.{column}" for column in scope_columns
        )
        bind.execute(
            sa.text(
                f"""
                DELETE FROM {table}
                WHERE league_id = :loser
                AND EXISTS (
                    SELECT 1
                    FROM {table} AS keeper_rows
                    WHERE keeper_rows.league_id = :keeper
                    AND {scope_match}
                )
                """
            ),
            {"keeper": keeper_id, "loser": loser_id},
        )


def _table_exists(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)
