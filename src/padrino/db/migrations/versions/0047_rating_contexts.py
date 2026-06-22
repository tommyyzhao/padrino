"""RatingContext substrate and additive canonical markers (US-171, US-223).

Adds first-class rating contexts keyed only by ``(ruleset_id, kind)``. The
existing scientific ``ratings`` / ``rating_events`` tables remain reached by
``League.kind`` plus the runner's fail-closed rating chokepoint; the new
``rating_context_id`` columns are additive markers, not a competing write path.

This migration is a frozen historical snapshot. It seeds/backfills only the two
canonical rulesets that had historical scientific rating rows when 0047 shipped:
``mini7_v1`` and ``bench10_v1``. Built-in rulesets introduced with or after this
revision are materialized at runtime through
``padrino.db.repositories.rating_contexts.ensure_declared_context``. If a future
ruleset ever needs a historical backfill, add a new forward migration with its
own frozen literals instead of importing live core declarations here.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-21
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FROZEN_0047_CREATED_AT = datetime(2026, 6, 21, tzinfo=UTC)
_FROZEN_0047_CANONICAL_CONTEXTS: dict[str, dict[str, Any]] = {
    "mini7_v1": {
        "id": uuid.UUID("04700000-0000-4000-8000-000000000007"),
        "kind": "CANONICAL_TEAM",
        "is_canonical": True,
        "display_label": "Mini 7 canonical team",
    },
    "bench10_v1": {
        "id": uuid.UUID("04700000-0000-4000-8000-000000000010"),
        "kind": "CANONICAL_TEAM",
        "is_canonical": True,
        "display_label": "Bench 10 canonical team",
    },
}


def _uuid_param(value: uuid.UUID) -> str:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return value.hex
    return str(value)


def _winner_from_terminal_result(raw: Any) -> str | None:
    if raw is None:
        return None
    payload: Any
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        payload = raw
    if not isinstance(payload, dict):
        return None
    winner = payload.get("winner")
    if winner in {"TOWN", "MAFIA", "DRAW"}:
        return str(winner)
    return None


def upgrade() -> None:
    op.create_table(
        "rating_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("is_canonical", sa.Boolean(), nullable=False),
        sa.Column("display_label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('CANONICAL_TEAM', 'PLACEMENT', 'SOLO_RATE')",
            name="ck_rating_context_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ruleset_id", "kind", name="uq_rating_context_ruleset_kind"),
    )
    _seed_existing_scientific_canonical_contexts()

    with op.batch_alter_table("ratings") as batch_op:
        batch_op.add_column(sa.Column("ruleset_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("rating_context_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_ratings_rating_context_id",
            "rating_contexts",
            ["rating_context_id"],
            ["id"],
        )

    with op.batch_alter_table("rating_events") as batch_op:
        batch_op.add_column(sa.Column("ruleset_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("rating_context_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("game_seed", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("team_outcome", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_rating_events_rating_context_id",
            "rating_contexts",
            ["rating_context_id"],
            ["id"],
        )

    _create_noncanonical_sibling_tables()
    _stamp_existing_canonical_rows()


def _seed_existing_scientific_canonical_contexts() -> None:
    bind = op.get_bind()
    ruleset_rows = bind.execute(
        sa.text(
            "SELECT DISTINCT ruleset_id FROM leagues WHERE kind = 'SCIENTIFIC' ORDER BY ruleset_id"
        )
    ).all()
    present_ruleset_ids = {str(row.ruleset_id) for row in ruleset_rows}
    rows = []
    for ruleset_id, context in _FROZEN_0047_CANONICAL_CONTEXTS.items():
        if ruleset_id not in present_ruleset_ids:
            continue
        rows.append(
            {
                "id": context["id"],
                "kind": context["kind"],
                "ruleset_id": ruleset_id,
                "is_canonical": context["is_canonical"],
                "display_label": context["display_label"],
                "created_at": _FROZEN_0047_CREATED_AT,
            }
        )
    if not rows:
        return

    table = sa.table(
        "rating_contexts",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("ruleset_id", sa.String()),
        sa.column("is_canonical", sa.Boolean()),
        sa.column("display_label", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(table, rows)


def _create_noncanonical_sibling_tables() -> None:
    op.create_table(
        "placement_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rating_context_id", sa.Uuid(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("mu", sa.Numeric(), nullable=False),
        sa.Column("sigma", sa.Numeric(), nullable=False),
        sa.Column("conservative_score", sa.Numeric(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("last_game_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.ForeignKeyConstraint(["rating_context_id"], ["rating_contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rating_context_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            name="uq_placement_rating_scope",
        ),
    )
    op.create_table(
        "placement_rating_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rating_context_id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("game_seed", sa.String(), nullable=False),
        sa.Column("team_outcome", sa.String(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=True),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("before_mu", sa.Numeric(), nullable=False),
        sa.Column("before_sigma", sa.Numeric(), nullable=False),
        sa.Column("after_mu", sa.Numeric(), nullable=False),
        sa.Column("after_sigma", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["rating_context_id"], ["rating_contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            "public_player_id",
            name="uq_placement_rating_event_scope",
        ),
    )
    op.create_table(
        "solo_rate_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rating_context_id", sa.Uuid(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("successes", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("posterior_alpha", sa.Numeric(), nullable=False),
        sa.Column("posterior_beta", sa.Numeric(), nullable=False),
        sa.Column("mean_success_rate", sa.Numeric(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.ForeignKeyConstraint(["rating_context_id"], ["rating_contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rating_context_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            name="uq_solo_rate_rating_scope",
        ),
    )
    op.create_table(
        "solo_rate_rating_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rating_context_id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("game_seed", sa.String(), nullable=False),
        sa.Column("outcome_label", sa.String(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=True),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("before_successes", sa.Integer(), nullable=False),
        sa.Column("before_attempts", sa.Integer(), nullable=False),
        sa.Column("after_successes", sa.Integer(), nullable=False),
        sa.Column("after_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["rating_context_id"], ["rating_contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            "public_player_id",
            name="uq_solo_rate_rating_event_scope",
        ),
    )


def _stamp_existing_canonical_rows() -> None:
    bind = op.get_bind()
    rating_rows = bind.execute(
        sa.text(
            "SELECT r.id, l.ruleset_id, rc.id AS context_id FROM ratings r "
            "JOIN leagues l ON l.id = r.league_id "
            "JOIN rating_contexts rc "
            "ON rc.ruleset_id = l.ruleset_id "
            "AND rc.kind = 'CANONICAL_TEAM' "
            "AND rc.is_canonical = true "
            "WHERE l.kind = 'SCIENTIFIC'"
        )
    ).all()
    for row in rating_rows:
        bind.execute(
            sa.text(
                "UPDATE ratings SET ruleset_id = :ruleset_id, "
                "rating_context_id = :context_id WHERE id = :id"
            ),
            {
                "ruleset_id": row.ruleset_id,
                "context_id": _coerce_context_id(row.context_id),
                "id": row.id,
            },
        )

    event_rows = bind.execute(
        sa.text(
            "SELECT re.id, l.ruleset_id, rc.id AS context_id, g.game_seed, "
            "g.terminal_result "
            "FROM rating_events re "
            "JOIN leagues l ON l.id = re.league_id "
            "JOIN rating_contexts rc "
            "ON rc.ruleset_id = l.ruleset_id "
            "AND rc.kind = 'CANONICAL_TEAM' "
            "AND rc.is_canonical = true "
            "JOIN games g ON g.id = re.game_id "
            "WHERE l.kind = 'SCIENTIFIC'"
        )
    ).all()
    for row in event_rows:
        bind.execute(
            sa.text(
                "UPDATE rating_events SET ruleset_id = :ruleset_id, "
                "rating_context_id = :context_id, game_seed = :game_seed, "
                "team_outcome = :team_outcome WHERE id = :id"
            ),
            {
                "ruleset_id": row.ruleset_id,
                "context_id": _coerce_context_id(row.context_id),
                "game_seed": row.game_seed,
                "team_outcome": _winner_from_terminal_result(row.terminal_result),
                "id": row.id,
            },
        )


def _coerce_context_id(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return _uuid_param(value)
    raw = str(value)
    if op.get_bind().dialect.name == "sqlite":
        return raw.replace("-", "")
    return raw


def downgrade() -> None:
    op.drop_table("solo_rate_rating_events")
    op.drop_table("solo_rate_ratings")
    op.drop_table("placement_rating_events")
    op.drop_table("placement_ratings")

    with op.batch_alter_table("rating_events") as batch_op:
        batch_op.drop_constraint("fk_rating_events_rating_context_id", type_="foreignkey")
        batch_op.drop_column("team_outcome")
        batch_op.drop_column("game_seed")
        batch_op.drop_column("rating_context_id")
        batch_op.drop_column("ruleset_id")

    with op.batch_alter_table("ratings") as batch_op:
        batch_op.drop_constraint("fk_ratings_rating_context_id", type_="foreignkey")
        batch_op.drop_column("rating_context_id")
        batch_op.drop_column("ruleset_id")

    op.drop_table("rating_contexts")
