"""Add rate_limit_buckets table for shared per-key rate limiting.

US-074. The in-process sliding-window limiter from US-056 multiplies the
effective ceiling when multiple uvicorn workers / replicas each maintain
their own counter. ``rate_limit_buckets`` backs a shared fixed-window
counter keyed by ``(api_keys.key_hash, window_start)`` so any worker can
read and increment the same bucket. ``window_start`` is the unix-epoch
second at which the 60s window began (i.e. ``int(now // window_seconds)
* window_seconds``).

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key_hash", "window_start"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
