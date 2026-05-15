"""Add games.created_at column.

US-055. The pagination cursor for every list endpoint is an opaque base64
``(created_at, id)`` tuple. Every other table already had ``created_at`` —
``games`` did not because v1 tracked only ``started_at`` (set when the runner
begins ticking). Adding ``created_at`` gives the list endpoint a stable
ordering key independent of when a game actually started executing.

Existing rows are backfilled to ``started_at`` when available, otherwise to
``completed_at``, otherwise to ``NOW()`` — the column is then made
``NOT NULL`` so future inserts must supply the value (the ORM default does
this).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE games "
        "SET created_at = COALESCE(started_at, completed_at, CURRENT_TIMESTAMP) "
        "WHERE created_at IS NULL"
    )
    with op.batch_alter_table("games") as batch:
        batch.alter_column("created_at", existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("created_at")
