"""Add ranked anti-cheat advisory flags.

Revision ID: 0064
Revises: 0063
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNIQUE_INDEX = "uq_ranked_flags_kind_principal_evidence_hash"
KIND_INDEX = "ix_ranked_flags_kind"
SEVERITY_INDEX = "ix_ranked_flags_severity"
PRINCIPAL_INDEX = "ix_ranked_flags_principal_id"


def upgrade() -> None:
    op.create_table(
        "ranked_flags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')",
            name="ck_ranked_flags_severity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        UNIQUE_INDEX,
        "ranked_flags",
        ["kind", "principal_id", "evidence_hash"],
        unique=True,
    )
    op.create_index(KIND_INDEX, "ranked_flags", ["kind"])
    op.create_index(SEVERITY_INDEX, "ranked_flags", ["severity"])
    op.create_index(PRINCIPAL_INDEX, "ranked_flags", ["principal_id"])


def downgrade() -> None:
    op.drop_index(PRINCIPAL_INDEX, table_name="ranked_flags")
    op.drop_index(SEVERITY_INDEX, table_name="ranked_flags")
    op.drop_index(KIND_INDEX, table_name="ranked_flags")
    op.drop_index(UNIQUE_INDEX, table_name="ranked_flags")
    op.drop_table("ranked_flags")
