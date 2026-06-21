"""Store moderated human chat text until delayed release (US-159).

US-159 moves human chat release out of the POST request and into the
human-aware tick's symmetric release schedule. The hold row therefore needs to
remember the moderation-approved text (including any soft mask) until the runner
flushes the phase to the sidecar.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("human_chat_submissions") as batch_op:
        batch_op.add_column(sa.Column("cleaned_text", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("human_chat_submissions") as batch_op:
        batch_op.drop_column("cleaned_text")
