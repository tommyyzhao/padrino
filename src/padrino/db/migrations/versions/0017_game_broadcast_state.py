"""Add broadcast_state column to games for spoiler-safe public broadcast index.

US-087. Tracks the broadcast lifecycle of a game independently of its true
``status``. Values: HIDDEN (default, not yet public), LIVE (currently
broadcasting — outcome must not be revealed), RECENT (broadcast complete —
outcome visible).

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("broadcast_state", sa.String(), nullable=False, server_default="HIDDEN"),
    )


def downgrade() -> None:
    op.drop_column("games", "broadcast_state")
