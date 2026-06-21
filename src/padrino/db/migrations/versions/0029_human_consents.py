"""Append-only human consent records for the one-tap consent + 16+ gate (US-130).

A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm 16+
(``AGE_GATE``) before sending any action or chat. ``human_consents`` is an
append-only audit trail: one combined tap records one row per document kind at
its current version, and a version bump appends fresh rows on re-acceptance.

Chains after the current Wave 9 head (0028). US-127 (the named dependency) is
0027; the chain has since advanced to 0028, so this revises 0028 to stay linear.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_consents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_principal_id", sa.Uuid(), nullable=False),
        sa.Column("document_kind", sa.String(), nullable=False),
        sa.Column("document_version", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_ip_hash", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["subject_principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("human_consents")
