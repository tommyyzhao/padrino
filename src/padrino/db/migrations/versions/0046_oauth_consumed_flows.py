"""Single-use OAuth flow ledger for replay resistance (US-202).

Records the per-flow unique token (the ``flow`` claim embedded in the signed
OAuth state) the moment the callback begins the code exchange, so a replayed
``(state cookie, code)`` pair fails closed independent of provider behavior. The
``flow`` token is the primary key; the callback inserts-or-rejects atomically
(``INSERT ... ON CONFLICT DO NOTHING``) before exchanging the code. Rows are
short-lived auth metadata (about the flow-cookie TTL) and prunable.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_consumed_flows",
        sa.Column("flow", sa.String(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("flow", name="pk_oauth_consumed_flows"),
    )


def downgrade() -> None:
    op.drop_table("oauth_consumed_flows")
