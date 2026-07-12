"""Persist immutable OpenRouter catalog snapshots and probe evidence.

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_sqlite_immutability_guards() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_catalog_snapshots_complete_immutable_update
        BEFORE UPDATE ON model_catalog_snapshots
        FOR EACH ROW WHEN OLD.status = 'COMPLETE'
        BEGIN
            SELECT RAISE(ABORT, 'catalog_snapshot_immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_snapshots_complete_immutable_delete
        BEFORE DELETE ON model_catalog_snapshots
        FOR EACH ROW WHEN OLD.status = 'COMPLETE'
        BEGIN
            SELECT RAISE(ABORT, 'catalog_snapshot_immutable');
        END
        """
    )
    for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        op.execute(
            f"""
            CREATE TRIGGER trg_catalog_entries_complete_immutable_{event.lower()}
            BEFORE {event} ON model_catalog_entries
            FOR EACH ROW WHEN EXISTS (
                SELECT 1 FROM model_catalog_snapshots
                WHERE id = {row}.catalog_snapshot_id AND status = 'COMPLETE'
            )
            BEGIN
                SELECT RAISE(ABORT, 'catalog_entry_immutable');
            END
            """
        )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_model_probe_evidence_append_only_{event.lower()}
            BEFORE {event} ON model_probe_evidence
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'model_probe_evidence_append_only');
            END
            """
        )


def _drop_sqlite_immutability_guards() -> None:
    for name in (
        "trg_model_probe_evidence_append_only_delete",
        "trg_model_probe_evidence_append_only_update",
        "trg_catalog_entries_complete_immutable_delete",
        "trg_catalog_entries_complete_immutable_update",
        "trg_catalog_entries_complete_immutable_insert",
        "trg_catalog_snapshots_complete_immutable_delete",
        "trg_catalog_snapshots_complete_immutable_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_postgresql_immutability_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION padrino_catalog_snapshot_immutable_guard()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'COMPLETE' THEN
                RAISE EXCEPTION 'catalog_snapshot_immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION padrino_catalog_entry_immutable_guard()
        RETURNS trigger AS $$
        DECLARE
            current_snapshot_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                current_snapshot_id := OLD.catalog_snapshot_id;
            ELSE
                current_snapshot_id := NEW.catalog_snapshot_id;
            END IF;
            IF EXISTS (
                SELECT 1 FROM model_catalog_snapshots
                WHERE id = current_snapshot_id AND status = 'COMPLETE'
            ) THEN
                RAISE EXCEPTION 'catalog_entry_immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION padrino_model_probe_evidence_append_only_guard()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'model_probe_evidence_append_only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_snapshots_complete_immutable "
        "BEFORE UPDATE OR DELETE ON model_catalog_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION padrino_catalog_snapshot_immutable_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_catalog_entries_complete_immutable "
        "BEFORE INSERT OR UPDATE OR DELETE ON model_catalog_entries "
        "FOR EACH ROW EXECUTE FUNCTION padrino_catalog_entry_immutable_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_model_probe_evidence_append_only "
        "BEFORE UPDATE OR DELETE ON model_probe_evidence "
        "FOR EACH ROW EXECUTE FUNCTION padrino_model_probe_evidence_append_only_guard()"
    )


def _drop_postgresql_immutability_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_model_probe_evidence_append_only ON model_probe_evidence"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_entries_complete_immutable ON model_catalog_entries"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_snapshots_complete_immutable ON model_catalog_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS padrino_model_probe_evidence_append_only_guard()")
    op.execute("DROP FUNCTION IF EXISTS padrino_catalog_entry_immutable_guard()")
    op.execute("DROP FUNCTION IF EXISTS padrino_catalog_snapshot_immutable_guard()")


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _create_sqlite_immutability_guards()
    elif dialect == "postgresql":
        _create_postgresql_immutability_guards()
    else:
        raise RuntimeError(f"unsupported catalog migration dialect: {dialect!r}")


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_immutability_guards()
    elif dialect == "postgresql":
        _drop_postgresql_immutability_guards()
    else:
        raise RuntimeError(f"unsupported catalog migration dialect: {dialect!r}")


def upgrade() -> None:
    op.create_table(
        "model_catalog_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_etag", sa.String(), nullable=True),
        sa.Column("source_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETE', 'ERROR')",
            name="ck_model_catalog_snapshots_status",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_model_catalog_snapshots_content_hash_len",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_catalog_snapshots_status_fetched_at",
        "model_catalog_snapshots",
        ["status", "fetched_at"],
    )
    op.create_table(
        "model_catalog_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_slug", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("modalities", sa.JSON(), nullable=False),
        sa.Column("context_length", sa.Integer(), nullable=True),
        sa.Column("pricing_strings", sa.JSON(), nullable=False),
        sa.Column("pricing_micros", sa.JSON(), nullable=False),
        sa.Column("supported_parameters", sa.JSON(), nullable=False),
        sa.Column("provider_summary", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_snapshot_id"],
            ["model_catalog_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_snapshot_id",
            "canonical_slug",
            name="uq_model_catalog_entries_snapshot_slug",
        ),
        sa.UniqueConstraint(
            "id",
            "catalog_snapshot_id",
            name="uq_model_catalog_entries_id_snapshot",
        ),
    )
    op.create_index(
        "ix_model_catalog_entries_canonical_slug",
        "model_catalog_entries",
        ["canonical_slug"],
    )
    op.create_index(
        "ix_model_catalog_entries_snapshot_expires_slug",
        "model_catalog_entries",
        ["catalog_snapshot_id", "expires_at", "canonical_slug"],
    )
    op.create_table(
        "model_probe_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("model_catalog_entry_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("probe_suite_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_snapshot_id"],
            ["model_catalog_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_catalog_entry_id", "catalog_snapshot_id"],
            ["model_catalog_entries.id", "model_catalog_entries.catalog_snapshot_id"],
            name="fk_model_probe_evidence_entry_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_snapshot_id",
            "model_catalog_entry_id",
            "prompt_hash",
            "schema_hash",
            "probe_suite_version",
            name="uq_model_probe_evidence_probe_key",
        ),
        sa.CheckConstraint(
            "length(prompt_hash) = 64",
            name="ck_model_probe_evidence_prompt_hash_len",
        ),
        sa.CheckConstraint(
            "length(schema_hash) = 64",
            name="ck_model_probe_evidence_schema_hash_len",
        ),
    )
    op.create_index(
        "ix_model_probe_evidence_entry_observed_at",
        "model_probe_evidence",
        ["model_catalog_entry_id", "observed_at"],
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index("ix_model_probe_evidence_entry_observed_at", table_name="model_probe_evidence")
    op.drop_table("model_probe_evidence")
    op.drop_index(
        "ix_model_catalog_entries_snapshot_expires_slug",
        table_name="model_catalog_entries",
    )
    op.drop_index("ix_model_catalog_entries_canonical_slug", table_name="model_catalog_entries")
    op.drop_table("model_catalog_entries")
    op.drop_index("ix_catalog_snapshots_status_fetched_at", table_name="model_catalog_snapshots")
    op.drop_table("model_catalog_snapshots")
