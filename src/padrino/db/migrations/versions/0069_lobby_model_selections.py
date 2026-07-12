"""Persist frozen canonical model selections for lobby AI seats.

Revision ID: 0069
Revises: 0068
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add an optional, schema-validated-by-service selection snapshot."""
    op.add_column(
        "lobby_seats",
        sa.Column("model_selection", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    """Remove the additive selection snapshot column."""
    op.drop_column("lobby_seats", "model_selection")
