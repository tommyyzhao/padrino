"""Add immutable pricing-basis metadata to LLM calls.

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.add_column(sa.Column("price_basis", sa.String(), nullable=True))
        batch.add_column(sa.Column("price_table_version", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("price_table_version")
        batch.drop_column("price_basis")
