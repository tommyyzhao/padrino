"""Add quarantine marker to ranked human ratings.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("human_rating") as batch_op:
        batch_op.add_column(sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("human_rating") as batch_op:
        batch_op.drop_column("quarantined_at")
