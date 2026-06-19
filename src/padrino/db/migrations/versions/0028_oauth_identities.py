"""OAuth identities for optional account sign-in (US-129).

A returning player can sign in with ONE OAuth provider so their stats persist
across sessions/devices. ``oauth_identities`` links a provider account
(``provider``, ``subject``) to an account :class:`~padrino.db.models.Principal`,
keyed uniquely on (provider, subject) so sign-in is find-or-create. No provider
tokens are persisted; there is no friends graph and no multi-account merge.

Chains after the current Wave 9 head (0027). US-127 (the named dependency) is
0027, so this revises 0027 to keep the chain linear.

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_identity_provider_subject"),
    )


def downgrade() -> None:
    op.drop_table("oauth_identities")
