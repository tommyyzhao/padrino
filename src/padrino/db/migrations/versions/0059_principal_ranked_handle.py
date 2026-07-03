"""Add pseudonymous ranked handles to principals.

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNIQUE_INDEX_NAME = "uq_principals_ranked_handle_normalized"


def upgrade() -> None:
    with op.batch_alter_table("principals") as batch_op:
        batch_op.add_column(sa.Column("ranked_handle", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("ranked_handle_normalized", sa.String(), nullable=True))
        batch_op.create_index(
            UNIQUE_INDEX_NAME,
            ["ranked_handle_normalized"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("principals") as batch_op:
        batch_op.drop_index(UNIQUE_INDEX_NAME)
        batch_op.drop_column("ranked_handle_normalized")
        batch_op.drop_column("ranked_handle")
