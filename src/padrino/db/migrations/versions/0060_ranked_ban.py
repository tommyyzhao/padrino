"""Create ranked-ban/suspension primitive.

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRINCIPAL_INDEX = "ix_ranked_ban_principal_id"
EMAIL_HASH_INDEX = "ix_ranked_ban_email_hash"


def upgrade() -> None:
    op.create_table(
        "ranked_ban",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("email_hash", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('ban', 'suspension')",
            name="ck_ranked_ban_kind",
        ),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(PRINCIPAL_INDEX, "ranked_ban", ["principal_id"])
    op.create_index(EMAIL_HASH_INDEX, "ranked_ban", ["email_hash"])


def downgrade() -> None:
    op.drop_index(EMAIL_HASH_INDEX, table_name="ranked_ban")
    op.drop_index(PRINCIPAL_INDEX, table_name="ranked_ban")
    op.drop_table("ranked_ban")
