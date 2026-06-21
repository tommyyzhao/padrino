"""Unique dormant humans-included league per ruleset (US-195).

A partial unique index scoped to ``kind = 'HUMANS_INCLUDED'`` prevents a
concurrent ``get_or_create_humans_included`` from materializing duplicate dormant
leagues for the same ruleset, without constraining the scientific leagues (which
legitimately repeat per ruleset). Existing rows stay byte-identical.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_league_humans_included_ruleset"
_WHERE = sa.text("kind = 'HUMANS_INCLUDED'")


def upgrade() -> None:
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
