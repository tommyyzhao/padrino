"""Add catalog-backed identity hashes for OpenRouter materialization.

Revision ID: 0068
Revises: 0067
Create Date: 2026-07-12
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inference_params_hash(value: object) -> str:
    """Hash legacy JSON without changing its stored payload or behavior."""
    parsed: object = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
    if not isinstance(parsed, Mapping):
        parsed = {"legacy_inference_params": parsed}
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _backfill_agent_build_param_hashes() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, inference_params FROM agent_builds")).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE agent_builds SET inference_params_hash = :inference_params_hash "
                "WHERE id = :agent_build_id"
            ),
            {
                "agent_build_id": row["id"],
                "inference_params_hash": _inference_params_hash(row["inference_params"]),
            },
        )


def upgrade() -> None:
    # SQLite cannot safely batch-recreate either parent table while existing
    # game/gauntlet rows reference it and foreign-key enforcement is on. Its
    # native ``ADD COLUMN`` supports this additive shape; portable unique
    # indexes keep the materialization key authoritative on both dialects.
    dialect = op.get_bind().dialect.name
    op.add_column("model_configs", sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=True))
    op.add_column("model_configs", sa.Column("catalog_canonical_slug", sa.String(), nullable=True))
    op.add_column(
        "model_configs", sa.Column("catalog_identity_hash", sa.String(length=64), nullable=True)
    )
    op.create_index(
        "uq_model_configs_catalog_identity_hash",
        "model_configs",
        ["catalog_identity_hash"],
        unique=True,
    )

    op.add_column("agent_builds", sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=True))
    op.add_column("agent_builds", sa.Column("qualification_tier", sa.String(), nullable=True))
    op.add_column(
        "agent_builds", sa.Column("inference_params_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "agent_builds",
        sa.Column("materialization_identity_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_agent_builds_materialization_identity_hash",
        "agent_builds",
        ["materialization_identity_hash"],
        unique=True,
    )

    if dialect == "postgresql":
        op.create_foreign_key(
            "fk_model_configs_catalog_snapshot_id",
            "model_configs",
            "model_catalog_snapshots",
            ["catalog_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_agent_builds_catalog_snapshot_id",
            "agent_builds",
            "model_catalog_snapshots",
            ["catalog_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    _backfill_agent_build_param_hashes()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_agent_builds_catalog_snapshot_id",
            "agent_builds",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_model_configs_catalog_snapshot_id",
            "model_configs",
            type_="foreignkey",
        )
    op.drop_index("uq_agent_builds_materialization_identity_hash", table_name="agent_builds")
    op.drop_column("agent_builds", "materialization_identity_hash")
    op.drop_column("agent_builds", "inference_params_hash")
    op.drop_column("agent_builds", "qualification_tier")
    op.drop_column("agent_builds", "catalog_snapshot_id")
    op.drop_index("uq_model_configs_catalog_identity_hash", table_name="model_configs")
    op.drop_column("model_configs", "catalog_identity_hash")
    op.drop_column("model_configs", "catalog_canonical_slug")
    op.drop_column("model_configs", "catalog_snapshot_id")
