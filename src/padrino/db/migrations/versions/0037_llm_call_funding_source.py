"""Add funding_source to the cost-tracking row (US-151).

Cost governance for human play needs every inference cost row to record who
funds it. ``PLATFORM`` is the byte-identical default (human play is
platform-absorbed within a Moderate budget in v1); ``BYOK_OWNER`` and
``SPONSOR_POOL`` are designed-now-dormant so the schema is forward-compatible
without a later migration.

Adds a NOT NULL ``funding_source`` column (server default ``PLATFORM``) to
``llm_calls`` so every pre-existing row is backfilled to the platform default.

Chains after the lobby invite token (0036).

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch_op:
        batch_op.add_column(
            sa.Column(
                "funding_source",
                sa.String(),
                nullable=False,
                server_default="PLATFORM",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch_op:
        batch_op.drop_column("funding_source")
