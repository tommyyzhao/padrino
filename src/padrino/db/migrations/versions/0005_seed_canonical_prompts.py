"""Seed canonical per-role-family prompts for mini7_v1 (US-052).

Inserts one ``prompt_versions`` row per :class:`~padrino.core.enums.RoleFamily`,
all with ``version='canonical_mini7_v1'``. Each row's ``system_prompt`` is the
bundled markdown shipped under ``padrino/llm/prompts/mini7_v1/``; the
``developer_prompt`` carries the role-family name so the rows are
distinguishable from each other without parsing JSON.

This is a data-only migration — no schema changes. Re-running ``alembic upgrade
head`` is a no-op because alembic tracks revision state; calling
:func:`upgrade` twice on the same database would violate the
``prompt_versions.prompt_hash`` uniqueness constraint, which is the desired
loud failure.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15

"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("id", sa.Uuid()),
        sa.column("ruleset_id", sa.String()),
        sa.column("version", sa.String()),
        sa.column("system_prompt", sa.String()),
        sa.column("developer_prompt", sa.String()),
        sa.column("response_schema", sa.JSON()),
        sa.column("prompt_hash", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "ruleset_id": template.ruleset_id,
            "version": template.version,
            "system_prompt": template.system_prompt,
            "developer_prompt": template.role_family.value,
            "response_schema": CANONICAL_RESPONSE_SCHEMA,
            "prompt_hash": template.prompt_hash,
            "created_at": now,
        }
        for template in iter_canonical_prompts()
    ]
    op.bulk_insert(prompt_versions, rows)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM prompt_versions WHERE version = 'canonical_mini7_v1'"))
