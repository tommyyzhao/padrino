"""Repository tests for scheduled_gauntlets (US-085): CRUD + name uniqueness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.repositories import scheduled_gauntlets as repo

_SPEC = {"league_id": "11111111-1111-1111-1111-111111111111", "roster": {"P01": "x"}}


async def test_create_get_and_get_by_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        obj = await repo.create(
            session,
            name="nightly",
            schedule_cron="0 2 * * *",
            roster_spec_json=_SPEC,
            n_games=3,
            cost_cap_usd=5.0,
        )
        sid = obj.id
    async with session_factory() as session:
        assert (await repo.get(session, sid)) is not None
        by_name = await repo.get_by_name(session, "nightly")
        assert by_name is not None and by_name.id == sid
        assert by_name.enabled is True
        assert by_name.n_games == 3


async def test_name_uniqueness(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        await repo.create(
            session,
            name="dup",
            schedule_cron="* * * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
        )
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await repo.create(
                session,
                name="dup",
                schedule_cron="* * * * *",
                roster_spec_json=_SPEC,
                n_games=1,
                cost_cap_usd=1.0,
            )


async def test_list_due_respects_enabled_and_next_run_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        # Due: next_run_at is NULL.
        await repo.create(
            session,
            name="null-next",
            schedule_cron="* * * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
            next_run_at=None,
        )
        # Due: next_run_at in the past.
        await repo.create(
            session,
            name="past",
            schedule_cron="* * * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
            next_run_at=now - timedelta(minutes=5),
        )
        # Not due: next_run_at in the future.
        await repo.create(
            session,
            name="future",
            schedule_cron="* * * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
            next_run_at=now + timedelta(minutes=5),
        )
        # Not due: disabled even though next_run_at is past.
        await repo.create(
            session,
            name="disabled",
            schedule_cron="* * * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
            enabled=False,
            next_run_at=now - timedelta(minutes=5),
        )
    async with session_factory() as session:
        due_names = {r.name for r in await repo.list_due(session, now=now)}
    assert due_names == {"null-next", "past"}


async def test_update_disable_and_mark_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        obj = await repo.create(
            session,
            name="s",
            schedule_cron="0 2 * * *",
            roster_spec_json=_SPEC,
            n_games=1,
            cost_cap_usd=1.0,
            next_run_at=now,
        )
        sid = obj.id

    async with session_factory() as session, session.begin():
        updated = await repo.update(session, sid, cost_cap_usd=9.0, schedule_cron="*/5 * * * *")
        assert updated is not None
        assert updated.cost_cap_usd == 9.0
        assert updated.schedule_cron == "*/5 * * * *"

    # mark_run is exercised end-to-end against a real gauntlet FK in
    # tests/scheduler/test_gauntlet_job.py; here we cover the soft-delete.
    async with session_factory() as session, session.begin():
        disabled = await repo.disable(session, sid)
        assert disabled is not None
        assert disabled.enabled is False
        assert disabled.next_run_at is None
