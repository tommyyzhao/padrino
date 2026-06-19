"""Human principals + sessions, and game_seats.occupant_principal_id (US-127).

Browser humans need an identity/session layer completely separate from the
existing API-key auth, and a game seat must be linkable to a human principal:

- ``principals`` (id, kind 'guest'|'account', display_name nullable, deleted_at
  nullable, created_at/updated_at) — the browser-human identity.
- ``human_sessions`` (id, principal_id FK, session_hash sha256 only, kind,
  issued_at, expires_at, revoked_at, last_seen_at) — a browser session; the
  opaque token is never persisted (only its sha256).
- ``game_seats.occupant_principal_id`` (nullable FK -> principals.id) so a HUMAN
  seat links to its occupying principal; AI / legacy rows stay byte-identical.

Chains after the current Wave 9 head (0026). US-121 (the named dependency) is
0023; the linear chain has since advanced to 0026, so this revises 0026 to keep
the chain linear.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "human_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("session_hash", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_hash", name="uq_human_session_hash"),
    )
    with op.batch_alter_table("game_seats") as batch_op:
        batch_op.add_column(sa.Column("occupant_principal_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_game_seats_occupant_principal_id",
            "principals",
            ["occupant_principal_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("game_seats") as batch_op:
        batch_op.drop_constraint("fk_game_seats_occupant_principal_id", type_="foreignkey")
        batch_op.drop_column("occupant_principal_id")
    op.drop_table("human_sessions")
    op.drop_table("principals")
