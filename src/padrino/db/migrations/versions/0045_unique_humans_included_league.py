"""Unique dormant humans-included league per ruleset (US-195, US-199, US-205).

A partial unique index scoped to ``kind = 'HUMANS_INCLUDED'`` prevents a
concurrent ``get_or_create_humans_included`` from materializing duplicate dormant
leagues for the same ruleset, without constraining the scientific leagues (which
legitimately repeat per ruleset).

US-199: the pre-0045 ``get_or_create_humans_included`` was a bare read-then-create
with no DB constraint, so a deployed DB can already contain duplicate
``HUMANS_INCLUDED`` leagues for the same ``ruleset_id``. Creating the unique index
on such a DB would raise a duplicate-key error and abort the upgrade. So
``upgrade()`` first deduplicates: per ``ruleset_id`` it keeps the earliest
``(created_at, id)`` row, removes loser-side league-scoped rating rows that would
collide under the keeper, repoints every remaining dependent FK to that keeper,
then deletes the duplicates — making the upgrade succeed even on a DB that
already has duplicates. (Existing single-league rows stay byte-identical.)

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_league_humans_included_ruleset"
_WHERE = sa.text("kind = 'HUMANS_INCLUDED'")

# Every table with a FK to leagues.id (column name is ``league_id`` in all of
# them). Duplicate HUMANS_INCLUDED leagues are repointed to the keeper across
# ALL of these before deletion, so no dependent row is ever orphaned and the
# delete never trips a FK constraint.
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


def _dedup_humans_included_leagues(bind: sa.engine.Connection) -> None:
    """Collapse duplicate HUMANS_INCLUDED leagues to one keeper per ruleset_id.

    Keeper = earliest ``(created_at, id)``. Loser-side rows whose scope would
    duplicate a keeper-side row are deleted before remaining dependent FK rows
    are repointed to the keeper, then the loser league rows are deleted. Runs
    identically on every dialect by using only portable SQLAlchemy core
    selects/updates/deletes.
    """
    rows = bind.execute(
        sa.text("SELECT id, ruleset_id, created_at FROM leagues WHERE kind = 'HUMANS_INCLUDED'")
    ).all()

    # Group by ruleset_id, ordering by (created_at, id) so the keeper is stable.
    by_ruleset: dict[str, list[sa.engine.Row[Any]]] = {}
    for row in rows:
        by_ruleset.setdefault(row.ruleset_id, []).append(row)

    for group in by_ruleset.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: (str(r.created_at), str(r.id)))
        keeper = ordered[0]
        losers = [r.id for r in ordered[1:]]

        for loser_id in losers:
            _delete_loser_scope_collisions(bind, keeper_id=keeper.id, loser_id=loser_id)
            for table, column in _DEPENDENT_LEAGUE_FKS:
                if not _table_exists(bind, table):
                    continue
                # table/column come from the fixed _DEPENDENT_LEAGUE_FKS
                # allowlist, never user input, so the f-string is safe.
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :keeper WHERE {column} = :loser"),
                    {"keeper": keeper.id, "loser": loser_id},
                )
            bind.execute(
                sa.text("DELETE FROM leagues WHERE id = :loser"),
                {"loser": loser_id},
            )


def _delete_loser_scope_collisions(
    bind: sa.engine.Connection, *, keeper_id: object, loser_id: object
) -> None:
    """Remove loser rating rows that would violate league-scoped unique keys."""
    for table, scope_columns in _LEAGUE_SCOPED_UNIQUE_TABLES:
        if not _table_exists(bind, table):
            continue
        scope_match = " AND ".join(
            f"keeper_rows.{column} = {table}.{column}" for column in scope_columns
        )
        # table/column names come from the fixed _LEAGUE_SCOPED_UNIQUE_TABLES
        # allowlist, never user input, so the f-string is safe.
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


def upgrade() -> None:
    bind = op.get_bind()
    _dedup_humans_included_leagues(bind)
    op.create_index(
        _INDEX_NAME,
        "leagues",
        ["ruleset_id"],
        unique=True,
        sqlite_where=_WHERE,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="leagues")
