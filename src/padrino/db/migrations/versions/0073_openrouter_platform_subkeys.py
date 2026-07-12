"""Add encrypted optional OpenRouter platform subkeys.

Revision ID: 0073
Revises: 0072
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist envelopes and safe management metadata for account subkeys."""
    op.create_table(
        "openrouter_platform_subkeys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.String(), nullable=False),
        sa.Column("upstream_key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_name", sa.String(), nullable=False),
        sa.Column("display_suffix", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("limit_micros", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allowed_models", sa.JSON(), nullable=False),
        sa.Column(
            "allowlist_enforced_upstream", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'REVOKED')",
            name="ck_openrouter_platform_subkeys_status",
        ),
        sa.CheckConstraint(
            "limit_micros >= 0",
            name="ck_openrouter_platform_subkeys_limit_non_negative",
        ),
        sa.CheckConstraint(
            "length(upstream_key_hash) = 64",
            name="ck_openrouter_platform_subkeys_upstream_hash_len",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_openrouter_platform_subkeys_request_hash_len",
        ),
        sa.CheckConstraint(
            "status != 'ACTIVE' OR (ciphertext IS NOT NULL AND nonce IS NOT NULL)",
            name="ck_openrouter_platform_subkeys_active_envelope",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_openrouter_platform_subkeys_principal_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_openrouter_platform_subkeys"),
    )
    op.create_index(
        "uq_openrouter_platform_subkeys_active_principal",
        "openrouter_platform_subkeys",
        ["principal_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_openrouter_platform_subkeys_principal_status",
        "openrouter_platform_subkeys",
        ["principal_id", "status"],
    )


def downgrade() -> None:
    """Remove optional subkey envelopes and their indexes."""
    op.drop_index(
        "ix_openrouter_platform_subkeys_principal_status",
        table_name="openrouter_platform_subkeys",
    )
    op.drop_index(
        "uq_openrouter_platform_subkeys_active_principal",
        table_name="openrouter_platform_subkeys",
    )
    op.drop_table("openrouter_platform_subkeys")
