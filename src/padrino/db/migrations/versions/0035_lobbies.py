"""Add lobby tables for private friend lobbies (US-147).

A host creates a private lobby and configures the human-multiplayer game it will
launch (ruleset/size, identity mode, theme pack, bot pre-pick vs auto-fill,
stakes pinned CASUAL). Three tables: ``lobbies`` (config + lifecycle + seed +
host + Humans-Included league + nullable game), ``lobby_members`` (host + invited
friends), and ``lobby_seats`` (the pre-launch seat layout). No public matchmaking
in v1.

Chains after the current Wave 9 head (0034).

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lobbies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("identity_mode", sa.String(), nullable=False, server_default="ANONYMOUS"),
        sa.Column("theme_pack_id", sa.String(), nullable=True),
        sa.Column("stakes", sa.String(), nullable=False, server_default="CASUAL"),
        sa.Column(
            "integrity_acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("lobby_seed", sa.String(), nullable=False),
        sa.Column("host_principal_id", sa.Uuid(), nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["host_principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "lobby_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lobby_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("is_host", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["lobby_id"], ["lobbies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lobby_id", "principal_id", name="uq_lobby_member"),
    )
    op.create_table(
        "lobby_seats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lobby_id", sa.Uuid(), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("seat_kind", sa.String(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("agent_build_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["lobby_id"], ["lobbies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["lobby_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lobby_id", "seat_index", name="uq_lobby_seat_index"),
    )


def downgrade() -> None:
    op.drop_table("lobby_seats")
    op.drop_table("lobby_members")
    op.drop_table("lobbies")
