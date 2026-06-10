"""Add version to agent_builds and last_game_at to ratings (US-099).

AgentBuild.version tracks the agent's user-facing version string; defaults
to "v1" for all existing rows. Rating.last_game_at records the wall-clock
time of the most recent game so sigma-decay can compute idle days.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_builds",
        sa.Column(
            "version",
            sa.String(),
            nullable=False,
            server_default="v1",
        ),
    )
    op.add_column(
        "ratings",
        sa.Column("last_game_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ratings", "last_game_at")
    op.drop_column("agent_builds", "version")
