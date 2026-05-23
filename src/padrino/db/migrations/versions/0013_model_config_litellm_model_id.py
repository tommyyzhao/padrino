"""Add optional LiteLLM model identifier to model configs.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_configs", sa.Column("litellm_model_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_configs", "litellm_model_id")
