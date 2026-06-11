"""Add is_broadcastable column to games for moderation gate enforcement.

US-094. All public broadcast surfaces (live SSE, live index, recent index)
must filter on this flag. Default is False (fail-closed): games are not
broadcastable until the moderation gate explicitly sets the flag.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("is_broadcastable", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("games", "is_broadcastable")
