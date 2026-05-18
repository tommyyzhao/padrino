"""Add ingested_games table and api_keys.submission_public_key.

US-062. Federated ingestion of signed game bundles produced by US-061 export.
``ingested_games`` is intentionally separate from ``games`` so locally-run
games and externally-submitted games never mix in the local leaderboard
calculus. ``submitter_key_id`` references the submitter's ``api_keys`` row;
``league_id`` / ``gauntlet_id`` are stored as opaque strings (not FKs into
local tables) because the originating gauntlet does not exist on the
ingesting server.

``api_keys.submission_public_key`` is the urlsafe-base64-encoded Ed25519
public key (32-byte raw) that the submitter signed their bundle with — set
once at key creation and used during ingestion to verify ``bundle.sig`` /
``signer_fingerprint``.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("submission_public_key", sa.String(), nullable=True))

    op.create_table(
        "ingested_games",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("league_id", sa.String(), nullable=True),
        sa.Column("gauntlet_id", sa.String(), nullable=True),
        sa.Column("tip_hash", sa.String(), nullable=False),
        sa.Column("signer_fingerprint", sa.String(), nullable=True),
        sa.Column("verification_status", sa.String(), nullable=False),
        sa.Column(
            "submitter_key_id",
            sa.Uuid(),
            sa.ForeignKey("api_keys.id"),
            nullable=True,
        ),
        sa.Column("bundle", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", name="uq_ingested_games_game_id"),
    )


def downgrade() -> None:
    op.drop_table("ingested_games")
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("submission_public_key")
