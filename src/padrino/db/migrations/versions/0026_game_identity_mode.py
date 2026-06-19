"""Per-game identity_mode column (US-126).

Identity mode is a per-game value defaulting to ``ANONYMOUS`` (fail-closed) and
frozen after game start. This revision adds ``games.identity_mode`` (NOT NULL,
server_default ``ANONYMOUS``) so every existing AI-only game row stays
byte-identical and behaves identically.

Chains after the current Wave 9 head (0025). US-123 (the named dependency) is
0024; the linear chain has since advanced to 0025, so this revises 0025 to keep
the chain linear.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(
            sa.Column(
                "identity_mode",
                sa.String(),
                nullable=False,
                server_default="ANONYMOUS",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_column("identity_mode")
