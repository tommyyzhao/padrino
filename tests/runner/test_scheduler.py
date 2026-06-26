"""US-054 — async gauntlet scheduler.

Covers the pending → running → completed transition, concurrency cap,
crash-recovery (stale RUNNING rows reset to PENDING on startup), and the
SIGTERM-style graceful shutdown via a stop event. Game execution is injected
so the suite does not depend on the deterministic mock-adapter scripts; the
scheduler's contract is "drain pending gauntlets, flip statuses correctly,
honor the semaphore, and heartbeat while running" — not "run mini7 games".
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import LogCapture

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.reducer import initial_state
from padrino.core.engine.replay import ReplayHashMismatchError
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.game_status import (
    GAME_STATUS_COMPLETED,
    GAME_STATUS_CREATED,
    GAME_STATUS_FAILED,
    GAME_STATUS_RUNNING,
)
from padrino.db.models import (
    BudgetReservationSlot,
    Campaign,
    CampaignPairing,
    GameEvent,
    Gauntlet,
    LlmCall,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import campaigns as campaigns_repo
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.db.repositories import (
    scheduler_heartbeats as scheduler_heartbeats_repo,
)
from padrino.economics.benchmark_admission import (
    GLOBAL_BENCHMARK_SCOPE_KEY,
    campaign_scope_key,
    game_binding_key,
)
from padrino.llm.adapter import LlmAdapter
from padrino.llm.mock import DeterministicMockAdapter, NoopMockAdapter
from padrino.llm.retry import RetryExhausted
from padrino.runner import scheduler as scheduler_module
from padrino.runner.game_runner import GameConfig, GamePersistence, GameResume, run_game
from padrino.runner.scheduler import (
    SchedulerOptions,
    run_scheduler,
    scheduler_options_from_settings,
)
from padrino.settings import Settings
from tests.conftest import make_town_win_script


async def _seed_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
    clone_count: int = 2,
    status: str = "PENDING",
    heartbeat_at: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Insert a gauntlet (with status and roster) plus ``clone_count`` games.

    Returns ``(gauntlet_id, [game_id, ...])``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v",
            system_prompt="s",
            developer_prompt="d",
            response_schema={"type": "object"},
            prompt_hash=f"{hash_prefix}-prompt",
        )
        league = await leagues_repo.create(
            session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            ab = await agent_builds_repo.create(
                session,
                display_name=f"b-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league.id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv.id,
            clone_count=clone_count,
            gauntlet_seed=f"{hash_prefix}-seed",
            ranked=False,
            status=status,
        )
        if heartbeat_at is not None:
            gauntlet.heartbeat_at = heartbeat_at
        if created_at is not None:
            gauntlet.created_at = created_at
        for i, ab_id in enumerate(builds):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, i, ab_id)
        game_ids: list[uuid.UUID] = []
        for i in range(clone_count):
            g = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"{hash_prefix}-g{i}",
                gauntlet_id=gauntlet.id,
            )
            game_ids.append(g.id)
        return gauntlet.id, game_ids


async def _seed_campaign_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
    clone_count: int = 1,
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        pv = await prompt_versions_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            version="v",
            system_prompt="s",
            developer_prompt="d",
            response_schema={"type": "object"},
            prompt_hash=f"{hash_prefix}-prompt",
        )
        league = await leagues_repo.create(
            session, name="campaign-lane", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            ab = await agent_builds_repo.create(
                session,
                display_name=f"campaign-b-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        campaign = Campaign(
            campaign_seed=f"{hash_prefix}-campaign-seed",
            ruleset_id=mini7_v1.RULESET_ID,
            league_id=league.id,
            format="MIRROR",
            player_count=mini7_v1.PLAYER_COUNT,
            per_model_game_target=clone_count,
            status=campaigns_repo.CAMPAIGN_STATUS_RUNNING,
            sigma_target=2.5,
            rank_stability_k=10,
        )
        session.add(campaign)
        await session.flush()
        gauntlet = await gauntlets_repo.create(
            session,
            campaign_id=campaign.id,
            league_id=league.id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv.id,
            clone_count=clone_count,
            gauntlet_seed=f"{hash_prefix}-seed",
            ranked=True,
            status="PENDING",
        )
        for i, ab_id in enumerate(builds):
            await gauntlets_repo.add_roster_slot(session, gauntlet.id, i, ab_id)
        cell = CampaignPairing(
            campaign_id=campaign.id,
            cell_index=0,
            roster_json=[str(build_id) for build_id in builds],
            status=campaigns_repo.CAMPAIGN_PAIRING_MATERIALIZED,
            gauntlet_id=gauntlet.id,
        )
        session.add(cell)
        game_ids: list[uuid.UUID] = []
        for i in range(clone_count):
            g = await games_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed=f"{hash_prefix}-g{i}",
                gauntlet_id=gauntlet.id,
            )
            game_ids.append(g.id)
        await session.flush()
        return gauntlet.id, game_ids, cell.id


class _FakeExecutor:
    """In-memory game executor: records calls, optionally sleeps, tracks concurrency."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.calls: list[tuple[GameConfig, GamePersistence, bool]] = []
        self._delay = delay_s
        self.in_flight = 0
        self.peak_concurrency = 0
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        async with self._lock:
            self.in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self.in_flight)
        try:
            self.calls.append((config, persistence, ranked))
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            # Flip the game row to COMPLETED so subsequent observability /
            # diagnostics behave like a real terminal run.
            async with persistence.session_factory() as session, session.begin():
                await games_repo.update_status(
                    session,
                    persistence.game_id,
                    status="COMPLETED",
                    terminal_result={"winner": "TOWN", "reason": "stub", "day_terminated": 0},
                )
        finally:
            async with self._lock:
                self.in_flight -= 1


class _BlockingExecutor:
    """Fake executor that holds admitted games active until the test releases them."""

    def __init__(self, *, expected_started: int) -> None:
        self.calls: list[tuple[GameConfig, GamePersistence, bool]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._expected_started = expected_started
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        del adapter
        async with self._lock:
            self.calls.append((config, persistence, ranked))
            if len(self.calls) == self._expected_started:
                self.started.set()
        await self.release.wait()
        async with persistence.session_factory() as session, session.begin():
            await games_repo.update_status(
                session,
                persistence.game_id,
                status=GAME_STATUS_COMPLETED,
                terminal_result={"winner": "TOWN", "reason": "stub", "day_terminated": 0},
            )


class _BlockingFailureExecutor:
    """Fake executor that holds one admitted game active, then fails it."""

    def __init__(self) -> None:
        self.calls: list[tuple[GameConfig, GamePersistence, bool]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        del adapter
        async with self._lock:
            self.calls.append((config, persistence, ranked))
            self.started.set()
        await self.release.wait()
        raise TimeoutError("provider timeout while budget capped")


class _FailingExecutor(_FakeExecutor):
    """Fake executor that fails exactly one selected child game."""

    def __init__(self, *, failing_game_id: uuid.UUID) -> None:
        super().__init__()
        self.failing_game_id = failing_game_id

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        if persistence.game_id == self.failing_game_id:
            self.calls.append((config, persistence, ranked))
            raise RuntimeError("injected child-game failure")
        await super().__call__(config, persistence, adapter, ranked)


class _FlakyExecutor(_FakeExecutor):
    """Fake executor that fails a selected game a fixed number of times."""

    def __init__(self, *, failing_game_id: uuid.UUID, failures_before_success: int) -> None:
        super().__init__()
        self.failing_game_id = failing_game_id
        self.failures_before_success = failures_before_success
        self.attempts_by_game: dict[uuid.UUID, int] = {}

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        attempt = self.attempts_by_game.get(persistence.game_id, 0) + 1
        self.attempts_by_game[persistence.game_id] = attempt
        if persistence.game_id == self.failing_game_id and attempt <= self.failures_before_success:
            self.calls.append((config, persistence, ranked))
            raise RuntimeError(f"injected child-game failure attempt {attempt}")
        await super().__call__(config, persistence, adapter, ranked)


class _AlwaysFailingExecutor(_FakeExecutor):
    """Fake executor that raises a generated exception on every attempt."""

    def __init__(
        self,
        *,
        failing_game_id: uuid.UUID,
        exc_factory: Callable[[int], Exception],
    ) -> None:
        super().__init__()
        self.failing_game_id = failing_game_id
        self.exc_factory = exc_factory
        self.attempts_by_game: dict[uuid.UUID, int] = {}

    async def __call__(
        self,
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        attempt = self.attempts_by_game.get(persistence.game_id, 0) + 1
        self.attempts_by_game[persistence.game_id] = attempt
        if persistence.game_id == self.failing_game_id:
            self.calls.append((config, persistence, ranked))
            raise self.exc_factory(attempt)
        await super().__call__(config, persistence, adapter, ranked)


def _adapter_factory_noop() -> LlmAdapter:
    return NoopMockAdapter()


def _game_created_body(game_id: uuid.UUID, game_seed: str) -> dict[str, Any]:
    return {
        "event_type": "GameCreated",
        "sequence": 0,
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "ruleset_id": mini7_v1.RULESET_ID,
            "game_id": str(game_id),
            "game_seed": game_seed,
            "player_count": mini7_v1.PLAYER_COUNT,
        },
    }


def _roles_assigned_body(game_seed: str) -> dict[str, Any]:
    return {
        "event_type": "RolesAssigned",
        "sequence": 1,
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "assignments": [
                {
                    "public_player_id": seat.public_player_id,
                    "seat_index": seat.seat_index,
                    "role": seat.role.value,
                    "faction": seat.faction.value,
                }
                for seat in assign_roles(game_seed, mini7_v1)
            ]
        },
    }


def _phase_started_body() -> dict[str, Any]:
    return {
        "event_type": "PhaseStarted",
        "sequence": 2,
        "phase": "DAY_1_DISCUSSION_ROUND_1",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
    }


async def _persist_resume_prefix(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    game_seed: str,
) -> None:
    event_log = EventLog()
    for body in (
        _game_created_body(game_id, game_seed),
        _roles_assigned_body(game_seed),
        _phase_started_body(),
    ):
        stored = event_log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=stored.body["event_type"],
            phase=stored.body["phase"],
            visibility=stored.body["visibility"],
            actor_player_id=stored.body["actor_player_id"],
            payload=stored.body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


def _town_win_adapter_for_seed(game_seed: str) -> DeterministicMockAdapter:
    seats = assign_roles(game_seed, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return DeterministicMockAdapter(
        make_town_win_script(
            mafia_ids=mafia,
            town_ids=town,
            doctor_id=doctor,
            detective_id=detective,
        )
    )


# US-118 flake burn-down: the scheduler tests previously polled the gauntlet
# status with a TIGHT iteration budget (e.g. ``for _ in range(200)`` ~= 2s),
# then set the stop event UNCONDITIONALLY. Under load that budget could expire
# before the gauntlet reached its terminal status, tripping ``stop`` early and
# making the post-run status assertions flaky. ``_drive_scheduler_until`` keeps
# the original, deliberately cooperative poll shape (sleep FIRST, then read —
# this yields the event loop / the single shared in-memory SQLite connection to
# the scheduler's drive+write coroutines on every iteration, which is why the
# scheduler actually makes progress) but raises the budget to a generous
# deterministic bound. It also NEVER trips ``stop`` until the target status is
# observed, so a slow-but-correct run finalizes instead of being cut short, and
# fails loudly (rather than hanging) if the bound is genuinely exceeded.

# Cooperative status polling. A coarse 50ms interval (vs the old 10ms) is
# deliberate: every watcher DB read contends for the single shared in-memory
# SQLite connection with the scheduler's drive/write coroutines, so polling less
# often lets the scheduler make progress far faster (a tight 10ms loop could
# starve it into a multi-second-to-stalled drive). 600 polls = ~30s budget:
# orders of magnitude more than a correct run needs (~1.5s) yet still bounded.
_SCHEDULER_POLL_INTERVAL_S = 0.05
_SCHEDULER_MAX_POLLS = 600
# Hard ceiling on the scheduler coroutine itself: even after ``stop`` is set, a
# genuinely wedged run (e.g. stuck inside ``_drive_gauntlet``) must FAIL LOUDLY
# rather than hang the whole suite. Comfortably larger than the watcher budget.
_SCHEDULER_RUN_TIMEOUT_S = 60.0


async def _drive_scheduler_until(
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_id: uuid.UUID,
    target_status: str,
    *,
    stop: asyncio.Event,
    run: Awaitable[None],
) -> None:
    """Run the scheduler until ``gauntlet_id`` reaches ``target_status``.

    The watcher sets ``stop`` once the gauntlet reaches the target status (or the
    generous poll budget is exhausted), at which point the scheduler returns. The
    scheduler coroutine is wrapped in a hard timeout so a wedged run surfaces as a
    failure instead of hanging; the watcher is bounded and always awaited clean.
    """

    async def _watch() -> None:
        for _ in range(_SCHEDULER_MAX_POLLS):
            await asyncio.sleep(_SCHEDULER_POLL_INTERVAL_S)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None and g.status == target_status:
                break
        stop.set()

    watcher = asyncio.create_task(_watch())
    try:
        await asyncio.wait_for(run, timeout=_SCHEDULER_RUN_TIMEOUT_S)
    finally:
        stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def test_pending_gauntlet_runs_and_finalizes_to_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="happy")
    executor = _FakeExecutor()
    stop = asyncio.Event()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    assert {call[1].game_id for call in executor.calls} == set(game_ids)
    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"
    assert g.completed_at is not None
    assert g.heartbeat_at is None


async def test_child_game_failure_isolated_and_logged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="partial",
        clone_count=3,
    )
    failing_game_id = game_ids[1]
    executor = _FailingExecutor(failing_game_id=failing_game_id)
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=1,
    )
    capture = LogCapture()
    original_config = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(processors=[capture], cache_logger_on_first_use=False)

    try:
        await _drive_scheduler_until(
            session_factory,
            gauntlet_id,
            "COMPLETED",
            stop=stop,
            run=run_scheduler(
                session_factory,
                concurrency=3,
                stop_event=stop,
                adapter_factory=_adapter_factory_noop,
                game_executor=executor,
                options=options,
            ),
        )
    finally:
        structlog.reset_defaults()
        structlog.configure(**original_config)

    assert {call[1].game_id for call in executor.calls} == set(game_ids)
    async with session_factory() as session:
        games = await games_repo.list_by_gauntlet(session, gauntlet_id)
    statuses = {game.id: game.status for game in games}
    assert statuses[failing_game_id] == GAME_STATUS_FAILED
    assert statuses[game_ids[0]] == GAME_STATUS_COMPLETED
    assert statuses[game_ids[2]] == GAME_STATUS_COMPLETED

    failure_events = [
        entry for entry in capture.entries if entry["event"] == "scheduler.game.failed"
    ]
    assert len(failure_events) == 1
    assert failure_events[0]["gauntlet_id"] == str(gauntlet_id)
    assert failure_events[0]["game_id"] == str(failing_game_id)
    assert failure_events[0]["error_type"] == "RuntimeError"
    assert failure_events[0]["attempts"] == 1


async def test_drive_gauntlet_does_not_complete_with_non_terminal_child(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="non-terminal-child",
        clone_count=2,
    )
    created_game_id, completed_game_id = game_ids

    async def executor(
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        del config, adapter, ranked
        if persistence.game_id == created_game_id:
            return
        async with persistence.session_factory() as session, session.begin():
            await games_repo.update_status(
                session,
                persistence.game_id,
                status=GAME_STATUS_COMPLETED,
                terminal_result={"winner": "TOWN", "reason": "stub", "day_terminated": 0},
            )

    await scheduler_module._drive_gauntlet(
        session_factory,
        gauntlet_id,
        semaphore=asyncio.Semaphore(2),
        adapter_factory=_adapter_factory_noop,
        game_executor=executor,
        options=SchedulerOptions(game_max_attempts=1),
        clock=lambda: datetime(2026, 6, 24, 12, tzinfo=UTC),
        sleeper=asyncio.sleep,
        worker_id="test-worker",
    )

    async with session_factory() as session:
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        games = await games_repo.list_by_gauntlet(session, gauntlet_id)

    assert gauntlet is not None
    assert gauntlet.status != GAME_STATUS_COMPLETED
    statuses = {game.id: game.status for game in games}
    assert statuses[created_game_id] == GAME_STATUS_RUNNING
    assert statuses[completed_game_id] == GAME_STATUS_COMPLETED


async def test_child_game_retry_succeeds_before_marking_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="retry-success",
        clone_count=1,
    )
    game_id = game_ids[0]
    executor = _FlakyExecutor(failing_game_id=game_id, failures_before_success=1)
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=2,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)

    assert game is not None
    assert game.status == GAME_STATUS_COMPLETED
    assert executor.attempts_by_game[game_id] == 2


def test_scheduler_exception_classification_maps_retryable_and_poison_errors() -> None:
    class _Probe(BaseModel):
        value: int

    with pytest.raises(ValidationError) as validation_error:
        _Probe.model_validate({"value": "not-an-int"})

    timeout = scheduler_module.classify_game_exception(TimeoutError("upstream timeout"))
    exhausted = scheduler_module.classify_game_exception(
        RetryExhausted(attempts=3, last_error=TimeoutError("provider timeout"))
    )
    replay_hash = scheduler_module.classify_game_exception(
        ReplayHashMismatchError(sequence=7, expected="expected", actual="actual")
    )
    validation = scheduler_module.classify_game_exception(validation_error.value)
    unknown = scheduler_module.classify_game_exception(RuntimeError("transient executor crash"))

    assert timeout.disposition is scheduler_module.GameExceptionDisposition.RETRYABLE
    assert timeout.last_error_kind == "provider_transient"
    assert exhausted.disposition is scheduler_module.GameExceptionDisposition.RETRYABLE
    assert exhausted.last_error_kind == "provider_transient"
    assert replay_hash.disposition is scheduler_module.GameExceptionDisposition.POISON
    assert replay_hash.last_error_kind == "replay_hash_mismatch"
    assert validation.disposition is scheduler_module.GameExceptionDisposition.POISON
    assert validation.last_error_kind == "validation_error"
    assert unknown.disposition is scheduler_module.GameExceptionDisposition.RETRYABLE
    assert unknown.last_error_kind == "unknown_retryable"


async def test_poison_child_game_failure_stops_after_one_attempt_and_stamps_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="poison-replay",
        clone_count=1,
    )
    game_id = game_ids[0]
    executor = _AlwaysFailingExecutor(
        failing_game_id=game_id,
        exc_factory=lambda _attempt: ReplayHashMismatchError(
            sequence=1,
            expected="expected",
            actual="actual",
        ),
    )
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=3,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)

    assert game is not None
    assert game.status == GAME_STATUS_FAILED
    assert executor.attempts_by_game[game_id] == 1
    assert game.last_error_kind == "replay_hash_mismatch"
    assert game.last_error is not None
    assert "replay hash mismatch" in game.last_error


async def test_retryable_child_game_failure_retries_with_injected_backoff_and_stamps_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="retry-timeout",
        clone_count=1,
    )
    game_id = game_ids[0]
    executor = _AlwaysFailingExecutor(
        failing_game_id=game_id,
        exc_factory=lambda attempt: TimeoutError(f"provider timeout attempt {attempt}"),
    )
    delays: list[float] = []

    async def sleeper(delay_s: float) -> None:
        delays.append(delay_s)
        await asyncio.sleep(0)

    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=3,
        game_retry_backoff_s=0.25,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
            sleeper=sleeper,
        ),
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)

    assert game is not None
    assert game.status == GAME_STATUS_FAILED
    assert executor.attempts_by_game[game_id] == 3
    assert delays == [0.25, 0.5]
    assert game.last_error_kind == "provider_transient"
    assert game.last_error == "provider timeout attempt 3"


async def test_campaign_owned_failed_gauntlet_dead_letters_cell(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids, cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="campaign-failed-cell",
        clone_count=1,
    )
    game_id = game_ids[0]
    executor = _AlwaysFailingExecutor(
        failing_game_id=game_id,
        exc_factory=lambda attempt: TimeoutError(f"provider timeout attempt {attempt}"),
    )
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=2,
        campaign_cell_max_attempts=1,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        cell = await session.get(CampaignPairing, cell_id)
        game = await games_repo.get(session, game_id)

    assert cell is not None
    assert cell.status == campaigns_repo.CAMPAIGN_PAIRING_DEAD_LETTER
    assert cell.attempt_count == 1
    assert cell.last_error == "provider_transient: provider timeout attempt 2"
    assert game is not None
    assert game.status == GAME_STATUS_FAILED
    assert game.last_error_kind == "provider_transient"
    assert executor.attempts_by_game[game_id] == 2


async def test_campaign_budget_halt_short_circuits_before_recording_cell_failure(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids, cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="campaign-budget-halt-before-failure",
        clone_count=2,
    )
    async with session_factory() as session, session.begin():
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        assert gauntlet is not None
        gauntlet.status = "RUNNING"

    async def fake_run_child_games(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return scheduler_module._ChildGameRunResult(
            failures=[
                scheduler_module._ScheduledGameFailure(
                    game_id=game_ids[0],
                    exception=TimeoutError("provider timeout during capped sibling"),
                    attempts=1,
                )
            ],
            budget_halts=[
                scheduler_module._ScheduledGameBudgetHalt(
                    game_id=game_ids[1],
                    reason="campaign_budget_cap_reached",
                )
            ],
        )

    failure_record_calls: list[dict[str, Any]] = []
    original_record_failure = campaigns_repo.record_materialized_cell_failure

    async def spy_record_failure(*args: Any, **kwargs: Any) -> Any:
        failure_record_calls.append(dict(kwargs))
        return await original_record_failure(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "_run_child_games", fake_run_child_games)
    monkeypatch.setattr(campaigns_repo, "record_materialized_cell_failure", spy_record_failure)

    budget_halted = await scheduler_module._drive_gauntlet(
        session_factory,
        gauntlet_id,
        semaphore=asyncio.Semaphore(2),
        adapter_factory=_adapter_factory_noop,
        game_executor=_FakeExecutor(),
        options=SchedulerOptions(campaign_cell_max_attempts=2),
        clock=lambda: datetime(2026, 6, 24, 12, tzinfo=UTC),
        sleeper=asyncio.sleep,
        worker_id="campaign-budget-halt-short-circuit",
    )

    async with session_factory() as session:
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        cell = await session.get(CampaignPairing, cell_id)

    assert budget_halted is True
    assert failure_record_calls == []
    assert gauntlet is not None
    assert gauntlet.status == "PENDING"
    assert cell is not None
    assert cell.status == campaigns_repo.CAMPAIGN_PAIRING_MATERIALIZED
    assert cell.attempt_count == 0
    assert cell.gauntlet_id == gauntlet_id


async def test_campaign_budget_halt_with_sibling_failure_preserves_cell_for_redrive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids, cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="campaign-budget-halt-redrive",
        clone_count=2,
    )
    async with session_factory() as session, session.begin():
        session.add(
            LlmCall(
                game_id=game_ids[0],
                public_player_id="P01",
                phase="DAY_1_DISCUSSION",
                request_json={},
                request_prompt_hash="prompt",
                status="ok",
                cost_usd=0.5,
            )
        )

    first_executor = _BlockingFailureExecutor()
    capped_options = SchedulerOptions(
        game_max_attempts=1,
        campaign_cell_max_attempts=1,
        padrino_global_spend_cap_usd=10.0,
        padrino_campaign_spend_cap_usd=1.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )
    first_drive = asyncio.create_task(
        scheduler_module._drive_gauntlet(
            session_factory,
            gauntlet_id,
            semaphore=asyncio.Semaphore(2),
            adapter_factory=_adapter_factory_noop,
            game_executor=first_executor,
            options=capped_options,
            clock=lambda: datetime(2026, 6, 24, 12, tzinfo=UTC),
            sleeper=asyncio.sleep,
            worker_id="campaign-budget-halt-redrive-1",
        )
    )
    await asyncio.wait_for(first_executor.started.wait(), timeout=_SCHEDULER_RUN_TIMEOUT_S)
    await asyncio.sleep(0.05)
    first_executor.release.set()
    budget_halted = await asyncio.wait_for(first_drive, timeout=_SCHEDULER_RUN_TIMEOUT_S)

    async with session_factory() as session:
        cell_after_pause = await session.get(CampaignPairing, cell_id)
        gauntlet_after_pause = await gauntlets_repo.get(session, gauntlet_id)
        games_after_pause = await games_repo.list_by_gauntlet(session, gauntlet_id)

    assert budget_halted is True
    assert len(first_executor.calls) == 1
    assert cell_after_pause is not None
    assert cell_after_pause.status == campaigns_repo.CAMPAIGN_PAIRING_MATERIALIZED
    assert cell_after_pause.attempt_count == 0
    assert cell_after_pause.gauntlet_id == gauntlet_id
    assert gauntlet_after_pause is not None
    assert gauntlet_after_pause.status == "PENDING"
    assert {game.status for game in games_after_pause} == {
        GAME_STATUS_CREATED,
        GAME_STATUS_FAILED,
    }

    second_executor = _FakeExecutor()
    open_options = SchedulerOptions(
        game_max_attempts=1,
        campaign_cell_max_attempts=1,
        padrino_global_spend_cap_usd=10.0,
        padrino_campaign_spend_cap_usd=10.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )
    completed = await scheduler_module._drive_gauntlet(
        session_factory,
        gauntlet_id,
        semaphore=asyncio.Semaphore(2),
        adapter_factory=_adapter_factory_noop,
        game_executor=second_executor,
        options=open_options,
        clock=lambda: datetime(2026, 6, 24, 12, 1, tzinfo=UTC),
        sleeper=asyncio.sleep,
        worker_id="campaign-budget-halt-redrive-2",
    )

    async with session_factory() as session:
        cell_after_redrive = await session.get(CampaignPairing, cell_id)
        assert cell_after_redrive is not None
        gauntlet_after_redrive = await gauntlets_repo.get(session, gauntlet_id)
        campaign = await session.get(Campaign, cell_after_redrive.campaign_id)
        assert campaign is not None
        games_after_redrive = await games_repo.list_by_gauntlet(session, gauntlet_id)
        campaign_gauntlet_count = await session.scalar(
            select(func.count(Gauntlet.id)).where(Gauntlet.campaign_id == campaign.id)
        )

    assert completed is False
    assert len(second_executor.calls) == 1
    assert cell_after_redrive.status == campaigns_repo.CAMPAIGN_PAIRING_COMPLETED
    assert cell_after_redrive.attempt_count == 0
    assert cell_after_redrive.gauntlet_id == gauntlet_id
    assert gauntlet_after_redrive is not None
    assert gauntlet_after_redrive.status == "COMPLETED"
    assert campaign.status == campaigns_repo.CAMPAIGN_STATUS_COMPLETED
    assert {game.status for game in games_after_redrive} == {
        GAME_STATUS_COMPLETED,
        GAME_STATUS_FAILED,
    }
    assert campaign_gauntlet_count == 1


async def test_scheduler_fresh_game_attempt_has_no_resume(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="fresh-resume",
        clone_count=1,
    )
    executor = _FakeExecutor()
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=1,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    assert len(executor.calls) == 1
    assert executor.calls[0][1].resume is None


async def test_scheduler_threads_worker_id_and_clock_to_game_persistence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = datetime(2026, 6, 24, 12, tzinfo=UTC)
    gauntlet_id, _game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="worker-fence",
        clone_count=1,
    )
    seen: list[tuple[str | None, datetime | None]] = []

    async def executor(
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        del config, adapter, ranked
        seen.append(
            (
                persistence.worker_id,
                persistence.lease_clock() if persistence.lease_clock is not None else None,
            )
        )
        async with persistence.session_factory() as session, session.begin():
            game = await games_repo.get(session, persistence.game_id)
            assert game is not None
            assert game.leased_by == "worker-fence-test"
            assert game.lease_expires_at is not None
            await games_repo.update_status(
                session,
                persistence.game_id,
                status=GAME_STATUS_COMPLETED,
                terminal_result={"winner": "TOWN", "reason": "stub", "day_terminated": 0},
            )

    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
            clock=lambda: base,
            worker_id="worker-fence-test",
        ),
    )

    assert seen == [("worker-fence-test", base)]


async def test_default_game_executor_threads_persistence_resume(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen: dict[str, GameResume | None] = {}
    resume = GameResume(state=initial_state(), event_log=EventLog(), phase="SETUP")
    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=uuid.uuid4(),
        resume=resume,
    )

    async def fake_run_game(
        config: GameConfig,
        adapter: LlmAdapter,
        ranked: bool,
        *,
        persistence: GamePersistence | None = None,
        resume: GameResume | None = None,
    ) -> None:
        del config, adapter, ranked, persistence
        seen["resume"] = resume

    monkeypatch.setattr(scheduler_module, "run_game", fake_run_game)

    await scheduler_module._default_game_executor(
        GameConfig(game_id="G-DEFAULT-RESUME", game_seed="seed"),
        persistence,
        NoopMockAdapter(),
        ranked=False,
    )

    assert seen["resume"] is resume


async def test_scheduler_retry_rehydrates_started_benchmark_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="retry-resume",
        clone_count=1,
    )
    game_id = game_ids[0]
    seen_resumes: list[GameResume | None] = []

    async def executor(
        config: GameConfig,
        persistence: GamePersistence,
        adapter: LlmAdapter,
        ranked: bool,
    ) -> None:
        del adapter, ranked
        seen_resumes.append(persistence.resume)
        if len(seen_resumes) == 1:
            async with session_factory() as session, session.begin():
                await _persist_resume_prefix(
                    session,
                    game_id=persistence.game_id,
                    game_seed=config.game_seed,
                )
            raise RuntimeError("simulated crash after persisted phase start")

        assert persistence.resume is not None
        await run_game(
            config,
            _town_win_adapter_for_seed(config.game_seed),
            False,
            persistence=persistence,
            resume=persistence.resume,
        )
        stop.set()

    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=2,
    )

    await asyncio.wait_for(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
        timeout=_SCHEDULER_RUN_TIMEOUT_S,
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            )
            .scalars()
            .all()
        )

    assert game is not None
    assert game.status == GAME_STATUS_COMPLETED
    assert [resume is None for resume in seen_resumes] == [True, False]
    assert seen_resumes[1] is not None
    assert seen_resumes[1].phase == "DAY_1_DISCUSSION_ROUND_1"
    assert [row.sequence for row in rows] == list(range(len(rows)))
    assert sum(row.event_type == "GameCreated" for row in rows) == 1
    assert sum(row.event_type == "RolesAssigned" for row in rows) == 1


async def test_child_game_exhausts_bounded_attempts_and_writes_no_ratings(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="retry-exhaust",
        clone_count=1,
    )
    game_id = game_ids[0]
    executor = _FlakyExecutor(failing_game_id=game_id, failures_before_success=99)
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=3,
    )

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)
        rating_rows = list((await session.execute(select(Rating))).scalars())
        rating_event_rows = list((await session.execute(select(RatingEvent))).scalars())

    assert game is not None
    assert game.status == GAME_STATUS_FAILED
    assert game.terminal_result is None
    assert game.completed_at is not None
    assert executor.attempts_by_game[game_id] == 3
    assert rating_rows == []
    assert rating_event_rows == []


async def test_failed_game_is_not_reselected_by_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="skip-failed",
        clone_count=2,
    )
    failed_game_id, runnable_game_id = game_ids
    async with session_factory() as session, session.begin():
        await games_repo.update_status(
            session,
            failed_game_id,
            status=GAME_STATUS_FAILED,
            completed_at=datetime.now(UTC),
        )
    executor = _FakeExecutor()
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        GAME_STATUS_COMPLETED,
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=2,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    assert [call[1].game_id for call in executor.calls] == [runnable_game_id]


def test_scheduler_options_are_settings_driven() -> None:
    settings = Settings(
        padrino_scheduler_game_max_attempts=5,
        padrino_enable_game_lease_reaper=True,
        padrino_game_lease_reaper_interval_seconds=17.0,
        padrino_game_lease_ttl_seconds=900.0,
    )

    options = scheduler_options_from_settings(settings)

    assert options.game_max_attempts == 5
    assert options.enable_game_lease_reaper is True
    assert options.game_lease_reaper_interval_s == 17.0
    assert options.game_lease_ttl_s == 900.0


def test_failed_game_status_literal_is_only_hardcoded_in_shared_status_module() -> None:
    root = Path("src/padrino")
    hits: list[tuple[str, int]] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"[\"']FAILED[\"']", text):
            hits.append((path.as_posix(), text.count("\n", 0, match.start()) + 1))

    assert {path for path, _line in hits} == {"src/padrino/db/game_status.py"}


async def test_concurrency_cap_honored_via_injected_semaphore(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(session_factory, hash_prefix="cap", clone_count=6)
    executor = _FakeExecutor(delay_s=0.05)
    semaphore = asyncio.Semaphore(2)
    stop = asyncio.Event()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=2,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            semaphore=semaphore,
            options=options,
        ),
    )

    assert len(executor.calls) == len(game_ids)
    assert executor.peak_concurrency <= 2


async def test_benchmark_budget_gate_bounds_concurrent_game_starts_and_retries_later(
    tmp_path: Path,
) -> None:
    from padrino.db.base import Base, create_engine, create_session_factory

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduler-budget.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="budget-global",
        clone_count=4,
    )
    executor = _BlockingExecutor(expected_started=2)
    options = SchedulerOptions(
        game_max_attempts=1,
        padrino_global_spend_cap_usd=1.0,
        padrino_campaign_spend_cap_usd=100.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )

    try:
        drive = asyncio.create_task(
            scheduler_module._drive_gauntlet(
                session_factory,
                gauntlet_id,
                semaphore=asyncio.Semaphore(4),
                adapter_factory=_adapter_factory_noop,
                game_executor=executor,
                options=options,
                clock=lambda: datetime(2026, 6, 24, 12, tzinfo=UTC),
                sleeper=asyncio.sleep,
                worker_id="budget-worker",
            )
        )
        await asyncio.wait_for(executor.started.wait(), timeout=_SCHEDULER_RUN_TIMEOUT_S)
        await asyncio.sleep(0.05)

        assert len(executor.calls) == 2

        executor.release.set()
        budget_halted = await asyncio.wait_for(drive, timeout=_SCHEDULER_RUN_TIMEOUT_S)

        started_game_ids = {call[1].game_id for call in executor.calls}
        async with session_factory() as session:
            games = await games_repo.list_by_gauntlet(session, gauntlet_id)
            gauntlet = await gauntlets_repo.get(session, gauntlet_id)
            live_slots = await session.scalar(
                select(func.count(BudgetReservationSlot.id)).where(
                    BudgetReservationSlot.scope_key == "global:benchmark",
                    BudgetReservationSlot.released_at.is_(None),
                )
            )
    finally:
        await engine.dispose()

    assert budget_halted is True
    assert started_game_ids.issubset(set(game_ids))
    assert {game.status for game in games if game.id in started_game_ids} == {GAME_STATUS_COMPLETED}
    assert {game.status for game in games if game.id not in started_game_ids} == {
        GAME_STATUS_CREATED
    }
    assert gauntlet is not None
    assert gauntlet.status == "PENDING"
    assert live_slots == 0


async def test_game_lease_reaper_releases_orphaned_budget_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids, _cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="reaper-orphan-slot",
        clone_count=1,
    )
    game_id = game_ids[0]
    binding_key = game_binding_key(game_id)
    async with session_factory() as session, session.begin():
        gauntlet = await gauntlets_repo.get(session, gauntlet_id)
        assert gauntlet is not None
        assert gauntlet.campaign_id is not None
        game = await games_repo.get(session, game_id)
        assert game is not None
        game.status = GAME_STATUS_RUNNING
        game.leased_by = "crashed-worker"
        game.lease_expires_at = datetime(2026, 6, 24, 11, 59, tzinfo=UTC)
        session.add_all(
            [
                BudgetReservationSlot(
                    scope_key=GLOBAL_BENCHMARK_SCOPE_KEY,
                    slot_index=0,
                    reserved_at=datetime(2026, 6, 24, 11, 58, tzinfo=UTC),
                    binding_key=binding_key,
                ),
                BudgetReservationSlot(
                    scope_key=campaign_scope_key(gauntlet.campaign_id),
                    slot_index=0,
                    reserved_at=datetime(2026, 6, 24, 11, 58, tzinfo=UTC),
                    binding_key=binding_key,
                ),
            ]
        )

    reset = await scheduler_module._reap_stale_games(
        session_factory,
        now=datetime(2026, 6, 24, 12, tzinfo=UTC),
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)
        live_slots = await session.scalar(
            select(func.count(BudgetReservationSlot.id)).where(
                BudgetReservationSlot.binding_key == binding_key,
                BudgetReservationSlot.released_at.is_(None),
            )
        )

    assert reset == [game_id]
    assert game is not None
    assert game.leased_by is None
    assert game.lease_expires_at is None
    assert live_slots == 0


async def test_campaign_budget_halt_skips_to_next_campaign_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    capped_gauntlet_id, capped_game_ids, _capped_cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="campaign-budget-capped",
        clone_count=1,
    )
    open_gauntlet_id, open_game_ids, _open_cell_id = await _seed_campaign_gauntlet(
        session_factory,
        hash_prefix="campaign-budget-open",
        clone_count=1,
    )
    async with session_factory() as session, session.begin():
        session.add(
            LlmCall(
                game_id=capped_game_ids[0],
                public_player_id="P01",
                phase="DAY_1_DISCUSSION",
                request_json={},
                request_prompt_hash="prompt",
                status="ok",
                cost_usd=1.0,
            )
        )

    executor = _FakeExecutor()
    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        game_max_attempts=1,
        padrino_global_spend_cap_usd=10.0,
        padrino_campaign_spend_cap_usd=1.0,
        padrino_benchmark_admission_reserve_usd=0.5,
    )

    await _drive_scheduler_until(
        session_factory,
        open_gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        capped_gauntlet = await gauntlets_repo.get(session, capped_gauntlet_id)
        capped_games = await games_repo.list_by_gauntlet(session, capped_gauntlet_id)

    assert {call[1].game_id for call in executor.calls} == set(open_game_ids)
    assert capped_gauntlet is not None
    assert capped_gauntlet.status == "PENDING"
    assert {game.status for game in capped_games} == {GAME_STATUS_CREATED}


async def test_crash_recovery_resets_stale_running_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A RUNNING row with a heartbeat older than 2x the interval simulates a
    # process that died mid-gauntlet.
    stale_hb = datetime.now(UTC) - timedelta(seconds=10)
    gauntlet_id, _game_ids = await _seed_gauntlet(
        session_factory,
        hash_prefix="recover",
        status="RUNNING",
        heartbeat_at=stale_hb,
    )
    executor = _FakeExecutor()
    stop = asyncio.Event()

    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "COMPLETED",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"
    # All games were re-executed after the reset.
    assert len(executor.calls) > 0


async def test_stop_event_finishes_in_flight_gauntlet_before_returning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, game_ids = await _seed_gauntlet(
        session_factory, hash_prefix="sigterm", clone_count=2
    )
    executor = _FakeExecutor(delay_s=0.1)
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    # Signal stop as soon as the gauntlet flips to RUNNING (mid-flight).
    await _drive_scheduler_until(
        session_factory,
        gauntlet_id,
        "RUNNING",
        stop=stop,
        run=run_scheduler(
            session_factory,
            concurrency=4,
            stop_event=stop,
            adapter_factory=_adapter_factory_noop,
            game_executor=executor,
            options=options,
        ),
    )

    assert len(executor.calls) == len(game_ids)
    async with session_factory() as session:
        g = await gauntlets_repo.get(session, gauntlet_id)
    assert g is not None
    assert g.status == "COMPLETED"


async def test_no_pending_gauntlets_returns_when_stop_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The loop must wake on stop_event even when no work is queued."""
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.5, heartbeat_interval_s=1.0, stale_factor=2.0)

    async def _signal() -> None:
        await asyncio.sleep(0.02)
        stop.set()

    setter = asyncio.create_task(_signal())
    await asyncio.wait_for(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            options=options,
        ),
        timeout=2.0,
    )
    await setter


async def test_worker_heartbeat_is_written_each_tick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scheduler must write its per-worker heartbeat to ``scheduler_heartbeats``."""
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    async def _signal() -> None:
        # Allow a couple of tick iterations so the heartbeat row exists.
        await asyncio.sleep(0.05)
        stop.set()

    setter = asyncio.create_task(_signal())
    await asyncio.wait_for(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            options=options,
            worker_id="test-worker:42",
        ),
        timeout=2.0,
    )
    await setter

    async with session_factory() as session:
        beats = await scheduler_heartbeats_repo.list_(session)
    assert len(beats) == 1
    assert beats[0].worker_id == "test-worker:42"
    assert beats[0].beat_at.tzinfo is not None


async def test_continuous_game_reaper_runs_during_loop_with_injected_clock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = datetime(2026, 6, 24, 12, tzinfo=UTC)
    current = base
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="continuous-game-reaper",
            status=GAME_STATUS_RUNNING,
        )
        game.leased_by = "dead-worker"
        game.lease_expires_at = base + timedelta(seconds=5)
        game_id = game.id

    def clock() -> datetime:
        return current

    async def _wait_for_worker_heartbeat() -> None:
        for _ in range(100):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                beats = await scheduler_heartbeats_repo.list_(session)
            if beats:
                return
        raise AssertionError("scheduler did not start ticking")

    async def _wait_for_game_lease_clear() -> None:
        for _ in range(100):
            await asyncio.sleep(0.01)
            async with session_factory() as session:
                row = await games_repo.get(session, game_id)
            if row is not None and row.leased_by is None and row.lease_expires_at is None:
                return
        raise AssertionError("scheduler did not reap the stale game lease")

    stop = asyncio.Event()
    options = SchedulerOptions(
        poll_interval_s=0.01,
        heartbeat_interval_s=0.05,
        stale_factor=2.0,
        enable_game_lease_reaper=True,
        game_lease_reaper_interval_s=1.0,
    )
    task = asyncio.create_task(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop,
            options=options,
            clock=clock,
            worker_id="game-reaper-test",
        )
    )

    try:
        await _wait_for_worker_heartbeat()
        async with session_factory() as session:
            before_advance = await games_repo.get(session, game_id)
        assert before_advance is not None
        assert before_advance.leased_by == "dead-worker"

        current = base + timedelta(seconds=6)
        await _wait_for_game_lease_clear()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_heartbeat_is_written_while_gauntlet_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _game_ids = await _seed_gauntlet(session_factory, hash_prefix="hb", clone_count=1)
    # Use enough delay so the heartbeat task fires at least once.
    executor = _FakeExecutor(delay_s=0.2)
    stop = asyncio.Event()
    options = SchedulerOptions(poll_interval_s=0.01, heartbeat_interval_s=0.05, stale_factor=2.0)

    observed_heartbeats: list[datetime] = []

    async def _observe() -> None:
        # Cooperative poll (sleep first, then read) so the scheduler's drive +
        # heartbeat coroutines get the shared in-memory SQLite connection each
        # iteration. Collect any in-flight heartbeats observed along the way;
        # stop once COMPLETED (or the generous budget is exhausted).
        for _ in range(_SCHEDULER_MAX_POLLS):
            await asyncio.sleep(_SCHEDULER_POLL_INTERVAL_S)
            async with session_factory() as session:
                g = await gauntlets_repo.get(session, gauntlet_id)
            if g is not None:
                if g.status == "RUNNING" and g.heartbeat_at is not None:
                    observed_heartbeats.append(g.heartbeat_at)
                if g.status == "COMPLETED":
                    break
        stop.set()

    waiter = asyncio.create_task(_observe())
    try:
        await asyncio.wait_for(
            run_scheduler(
                session_factory,
                concurrency=1,
                stop_event=stop,
                adapter_factory=_adapter_factory_noop,
                game_executor=executor,
                options=options,
            ),
            timeout=_SCHEDULER_RUN_TIMEOUT_S,
        )
    finally:
        stop.set()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter

    assert len(observed_heartbeats) >= 1
