"""Durable phase-specific human cadence tests (US-338)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.cadence import CASUAL_STANDARD_CADENCE_ID, cadence_profile
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase
from padrino.core.enums import PhaseKind
from padrino.runner import human_lane
from padrino.runner.game_runner import GameResume


def _discussion_state() -> GameState:
    return GameState(
        ruleset_id="custom:test",
        game_id="G-CADENCE",
        game_seed="cadence-seed",
        current_phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=2),
        seats=(),
        day=2,
    )


async def test_snapshotter_uses_injected_now_and_phase_specific_deadline(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    state = _discussion_state()
    now = [datetime(2026, 7, 11, 20, 0, tzinfo=UTC)]
    writes: list[tuple[str, datetime | None, datetime]] = []

    async def capture_snapshot(
        _session_factory: async_sessionmaker[AsyncSession],
        *,
        game_id: uuid.UUID,
        phase: str,
        deadline_at: datetime | None,
        updated_at: datetime,
        state: GameState,
        event_log: EventLog,
    ) -> None:
        del _session_factory, game_id, state, event_log
        writes.append((phase, deadline_at, updated_at))

    monkeypatch.setattr(human_lane, "persist_human_runtime_snapshot", capture_snapshot)
    snapshotter = human_lane._RuntimeSnapshotter(
        session_factory,
        game_id=uuid.uuid4(),
        phase_deadline_seconds=999,
        cadence=cadence_profile(CASUAL_STANDARD_CADENCE_ID),
        resume=None,
        now=lambda: now[0],
    )

    await snapshotter(state, EventLog(), "DAY_2_DISCUSSION_ROUND_2")

    assert writes == [
        (
            "DAY_2_DISCUSSION_ROUND_2",
            now[0] + timedelta(seconds=60),
            now[0],
        )
    ]
    now[0] += timedelta(seconds=11)
    config = snapshotter.tick_config(state)
    assert config.phase_deadline_seconds == 49
    assert config.release_delay_seconds == 3


async def test_restart_preserves_existing_deadline_without_extension(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    state = _discussion_state()
    event_log = EventLog()
    deadline = datetime(2026, 7, 11, 20, 1, tzinfo=UTC)
    now = [deadline - timedelta(seconds=17)]
    persisted: list[datetime | None] = []

    async def capture_snapshot(
        _session_factory: async_sessionmaker[AsyncSession],
        **kwargs: Any,
    ) -> None:
        del _session_factory
        persisted.append(kwargs["deadline_at"])

    monkeypatch.setattr(human_lane, "persist_human_runtime_snapshot", capture_snapshot)
    resume = GameResume(
        state=state,
        event_log=event_log,
        phase="DAY_2_DISCUSSION_ROUND_2",
        deadline_at=deadline,
        buffer_snapshot={},
    )
    snapshotter = human_lane._RuntimeSnapshotter(
        session_factory,
        game_id=uuid.uuid4(),
        phase_deadline_seconds=999,
        cadence=cadence_profile(CASUAL_STANDARD_CADENCE_ID),
        resume=resume,
        now=lambda: now[0],
    )

    await snapshotter(state, event_log, resume.phase)

    assert persisted == [deadline]
    assert snapshotter.tick_config(state).phase_deadline_seconds == 17
    now[0] = deadline + timedelta(seconds=5)
    assert snapshotter.tick_config(state).phase_deadline_seconds == 0
