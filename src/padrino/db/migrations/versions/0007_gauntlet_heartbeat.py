"""Add gauntlets.heartbeat_at column.

US-054. The async scheduler writes ``heartbeat_at`` every few seconds while a
gauntlet is in flight so a process crash leaves a stale timestamp that crash
recovery can detect on startup and re-enqueue.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("gauntlets") as batch:
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gauntlets") as batch:
        batch.drop_column("heartbeat_at")
