"""Add llm_calls.error_kind + llm_calls.error_message columns.

US-053. The retry loop classifies the final failure (e.g. ``RateLimitError``,
``TimeoutError``) and persists both the kind and the human-readable message so
audit / cost analysis can distinguish exhausted-retry calls from one-shot
non-retryable failures without parsing the legacy free-text ``error`` column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.add_column(sa.Column("error_kind", sa.String(), nullable=True))
        batch.add_column(sa.Column("error_message", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("error_message")
        batch.drop_column("error_kind")
