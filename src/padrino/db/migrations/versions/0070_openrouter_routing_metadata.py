"""Persist safe OpenRouter routing metadata on LLM call rows.

Revision ID: 0070
Revises: 0069
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable router-attribution fields without changing legacy calls."""
    with op.batch_alter_table("llm_calls") as batch:
        batch.add_column(sa.Column("openrouter_requested_model_slug", sa.String(), nullable=True))
        batch.add_column(sa.Column("openrouter_actual_model", sa.String(), nullable=True))
        batch.add_column(sa.Column("openrouter_actual_provider", sa.String(), nullable=True))
        batch.add_column(sa.Column("openrouter_actual_endpoint", sa.String(), nullable=True))
        batch.add_column(sa.Column("openrouter_generation_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("openrouter_routing_metadata", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("openrouter_catalog_snapshot_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_llm_calls_openrouter_catalog_snapshot_id",
            "model_catalog_snapshots",
            ["openrouter_catalog_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.add_column(sa.Column("openrouter_routing_policy_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    """Remove the additive OpenRouter router-attribution fields."""
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("openrouter_routing_policy_hash")
        batch.drop_constraint("fk_llm_calls_openrouter_catalog_snapshot_id", type_="foreignkey")
        batch.drop_column("openrouter_catalog_snapshot_id")
        batch.drop_column("openrouter_routing_metadata")
        batch.drop_column("openrouter_generation_id")
        batch.drop_column("openrouter_actual_endpoint")
        batch.drop_column("openrouter_actual_provider")
        batch.drop_column("openrouter_actual_model")
        batch.drop_column("openrouter_requested_model_slug")
