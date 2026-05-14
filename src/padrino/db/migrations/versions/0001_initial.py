"""Initial schema: core build / league / gauntlet / game tables.

Revision ID: 0001
Revises:
Create Date: 2026-05-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=True),
        sa.Column("auth_secret_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "model_configs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=True),
        sa.Column("default_temperature", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("default_top_p", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("default_max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("supports_structured_outputs", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["model_providers.id"]),
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("system_prompt", sa.String(), nullable=False),
        sa.Column("developer_prompt", sa.String(), nullable=False),
        sa.Column("response_schema", sa.JSON(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("prompt_hash"),
    )

    op.create_table(
        "agent_builds",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("model_config_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_version", sa.String(), nullable=False),
        sa.Column("inference_params", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
    )

    op.create_table(
        "leagues",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("ranked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "gauntlets",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=False),
        sa.Column("clone_count", sa.Integer(), nullable=False),
        sa.Column("gauntlet_seed", sa.String(), nullable=False),
        sa.Column("ranked", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
    )

    op.create_table(
        "gauntlet_roster_slots",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("gauntlet_id", sa.Uuid(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["gauntlet_id"], ["gauntlets.id"]),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.UniqueConstraint("gauntlet_id", "slot_index", name="uq_gauntlet_slot"),
    )

    op.create_table(
        "games",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("gauntlet_id", sa.Uuid(), nullable=True),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("game_seed", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("terminal_result", sa.String(), nullable=True),
        sa.Column("terminal_reason", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_phase", sa.String(), nullable=True),
        sa.Column("event_hash_head", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["gauntlet_id"], ["gauntlets.id"]),
    )

    op.create_table(
        "game_seats",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("faction", sa.String(), nullable=False),
        sa.Column("alive", sa.Boolean(), nullable=False),
        sa.Column("death_phase", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.UniqueConstraint("game_id", "public_player_id", name="uq_game_seat_public_id"),
        sa.UniqueConstraint("game_id", "seat_index", name="uq_game_seat_index"),
    )


def downgrade() -> None:
    op.drop_table("game_seats")
    op.drop_table("games")
    op.drop_table("gauntlet_roster_slots")
    op.drop_table("gauntlets")
    op.drop_table("leagues")
    op.drop_table("agent_builds")
    op.drop_table("prompt_versions")
    op.drop_table("model_configs")
    op.drop_table("model_providers")
