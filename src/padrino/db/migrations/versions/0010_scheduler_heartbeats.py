"""Add scheduler_heartbeats table for per-worker scheduler health.

US-060. One row per scheduler worker (``worker_id`` from
``socket.gethostname() + os.getpid()``) carrying the last beat timestamp.
The ``/healthz/scheduler`` endpoint reads ``MAX(beat_at)`` across all rows
to decide between ``ok``, ``degraded``, and ``down`` states. Distinct from
``gauntlets.heartbeat_at`` (US-054) which is the per-gauntlet liveness
column used by crash recovery.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_heartbeats",
        sa.Column("worker_id", sa.String(), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )


def downgrade() -> None:
    op.drop_table("scheduler_heartbeats")
