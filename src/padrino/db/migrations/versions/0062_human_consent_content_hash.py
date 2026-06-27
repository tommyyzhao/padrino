"""Bind human consent rows to legal document content hashes.

Revision ID: 0062
Revises: 0060
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("human_consents") as batch_op:
        batch_op.add_column(sa.Column("content_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("human_consents") as batch_op:
        batch_op.drop_column("content_hash")
