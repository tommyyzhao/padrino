"""Persist encrypted provider-credential and OpenRouter PKCE lifecycle state.

Revision ID: 0071
Revises: 0070
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add encrypted connection envelopes and PKCE replay-resistant state."""
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.String(), nullable=False),
        sa.Column("display_suffix", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revoked_at IS NOT NULL OR (ciphertext IS NOT NULL AND nonce IS NOT NULL)",
            name="ck_provider_credentials_active_envelope",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_provider_credentials_principal_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_credentials"),
    )
    op.create_index(
        "uq_provider_credentials_active_principal_provider",
        "provider_credentials",
        ["principal_id", "provider"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "openrouter_pkce_flows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("verifier_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(state_hash) = 64",
            name="ck_openrouter_pkce_flows_state_hash_len",
        ),
        sa.CheckConstraint(
            "length(verifier_binding_hash) = 64",
            name="ck_openrouter_pkce_flows_verifier_binding_hash_len",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_openrouter_pkce_flows_principal_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_openrouter_pkce_flows"),
        sa.UniqueConstraint("state_hash", name="uq_openrouter_pkce_flows_state_hash"),
    )


def downgrade() -> None:
    """Remove the additive credential and PKCE tables."""
    op.drop_table("openrouter_pkce_flows")
    op.drop_index(
        "uq_provider_credentials_active_principal_provider",
        table_name="provider_credentials",
    )
    op.drop_table("provider_credentials")
