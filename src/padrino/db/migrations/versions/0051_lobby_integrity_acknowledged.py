"""Add lobby integrity acknowledgement column additively (US-244).

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lobbies",
        sa.Column(
            "integrity_acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.drop_column("integrity_acknowledged")
