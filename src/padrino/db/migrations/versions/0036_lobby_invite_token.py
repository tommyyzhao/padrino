"""Add lobby invite token for invite links + presence (US-148).

A host shares an invite link so friends (guest or signed-in) can join a private
lobby. The invite is an opaque, shareable address for the lobby; single-use is
enforced per-person by membership, not by the token. Adds a unique
``invite_token`` column to ``lobbies``.

The column is NOT NULL, but every pre-existing lobby row gets a unique value
backfilled in the same migration (lobbies are short-lived friend lobbies; there
is no production data to preserve, but the backfill keeps the upgrade total).

Chains after the lobby tables (0035).

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-19
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add nullable first, backfill a unique token per existing row, then enforce
    # NOT NULL + UNIQUE (SQLite needs the batch rebuild for the constraint).
    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.add_column(sa.Column("invite_token", sa.String(), nullable=True))

    bind = op.get_bind()
    lobbies = sa.table(
        "lobbies", sa.column("id", sa.Uuid()), sa.column("invite_token", sa.String())
    )
    rows = bind.execute(sa.select(lobbies.c.id)).fetchall()
    for (lobby_id,) in rows:
        bind.execute(
            sa.update(lobbies)
            .where(lobbies.c.id == lobby_id)
            .values(invite_token=secrets.token_urlsafe(24))
        )

    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.alter_column("invite_token", existing_type=sa.String(), nullable=False)
        batch_op.create_unique_constraint("uq_lobby_invite_token", ["invite_token"])


def downgrade() -> None:
    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.drop_constraint("uq_lobby_invite_token", type_="unique")
        batch_op.drop_column("invite_token")
