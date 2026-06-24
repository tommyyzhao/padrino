"""Suite-blocking human-multiplayer invariant gates (Wave 9, US-146).

The two NON-NEGOTIABLE Wave-9 invariants are promoted here to comprehensive,
default-suite gates so no future change can silently regress them:

ANONYMITY
    A completed human game provably leaks ZERO human-vs-AI / model-identity
    markers on ANY pre-reveal surface: every live frame, the public_event_v1
    projection, the spectator projection, the per-seat observation guard, and
    the export bundle. The endgame reveal is the ONLY surface that discloses the
    truth, and only once the game is terminal (RECENT) — a LIVE game 404s.

SEGREGATION
    A completed human-lane game writes ZERO rows to the scientific ``ratings`` /
    ``rating_events`` tables (and zero to the dormant human-rating siblings in
    casual v1).

PRODUCTION WIRING
    The human lane uses the DB-backed ``HumanAdapter`` + ``run_human_tick``
    executor path. Human chat release timing, hydrated AI observations, and
    private participant reveal are all asserted here so the default suite
    catches regressions that isolated unit seams would miss.

This module *aggregates* the property scaffolds from US-124
(:mod:`tests.core.test_anonymity_property`) and US-125
(:mod:`tests.ratings.test_segregation`) and exercises them end-to-end against a
real, persisted, mixed human+AI game driven to terminal by the production
human-lane executor. The fixture uses deterministic adapters and pre-seeded
human POST-channel actions, so the whole gate stays deterministic without
touching a real LLM.
"""

from __future__ import annotations

import ast
import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, Faction, IdentityMode, Role, SeatKind
from padrino.core.human_chat import human_chat_content_ref
from padrino.core.observation_privacy import (
    HUMAN_IDENTITY_KEYS,
    IDENTITY_MARKER_KEYS,
    AnonymityViolation,
    assert_anonymous_safe,
    assert_no_identity_markers,
    project_game_row,
    project_seat_row,
)
from padrino.core.observations import Observation, Ruleset
from padrino.core.rulesets import mini7_v1
from padrino.core.rulesets.canonicality import assert_ruleset_canonical_pure
from padrino.core.spectator_projection import project_events_for_spectator
from padrino.db.models import (
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatMessage,
    HumanChatSubmission,
    HumanRating,
    HumanRatingEvent,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.export.bundle import assert_bundle_payload_safe, export_game
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.public.broadcast_index import BroadcastState
from padrino.public.live_tail import LiveTailConfig, stream_live_tail
from padrino.public.projection import to_public_events_v1
from padrino.runner import human_lane as human_lane_module
from padrino.runner.human_chat_release import release_held_chat_for_phase
from padrino.runner.human_durability import replay_state_from_rows
from padrino.runner.human_lane import (
    AiAdapterFactory,
    build_human_lane_adapter,
)
from padrino.runner.human_tick import (
    Clock,
    HumanTickConfig,
    HumanTickResult,
    Sleep,
)
from padrino.runner.human_tick import (
    run_human_tick as _real_run_human_tick,
)
from padrino.runner.tick import run_tick
from padrino.settings import Settings, get_settings
from tests.conftest import make_town_win_script

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "padrino"
_GAME_SEED = "seed-invariants-human-mp-001"
_RULESET = mini7_v1.RULESET_ID
_HUMAN_SEAT = "P01"
_DISCUSSION_PHASE = "DAY_1_DISCUSSION_ROUND_1"

# run_tick enforces the phase deadline with a REAL wall-clock
# ``asyncio.wait_for`` around ``adapter.complete`` (it is not driven by the
# injected FakeClock). The scripted/capture adapters answer in well under a
# millisecond, so this value is only a ceiling — but a tight 50ms ceiling is
# spuriously missed when the full ``--postgres`` suite runs under CPU/IO load on
# a CI runner, coercing the AI seat to a timed-out safe action (no public
# message) and dropping the asserted release. A generous ceiling removes the
# flake with zero happy-path cost (the deadline is never actually hit). The
# failure was Linux-CI-only; an idle macOS box always made the 50ms barrier.
_TICK_DEADLINE_S = 30.0


# --------------------------------------------------------------------------- #
# Mixed human+AI driving (US-139 pattern, deterministic clock)
# --------------------------------------------------------------------------- #


class _ScriptedSeatAdapter:
    """An LLM adapter returning one seat's slice of a phase-keyed script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: resp for key, resp in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


class _CaptureAdapter:
    """AI adapter that records the exact observation it received."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        self.observations.append(observation)
        response = AgentResponse(
            public_message=None,
            private_message=None,
            action=Action(type=ActionType.NOOP, target=None),
            memory_update="",
            rationale_summary=None,
        )
        return AdapterResult(
            raw_response=response.model_dump_json(),
            parsed_response=response,
            latency_ms=0,
        )


class _FakeClock:
    """Monotonic clock advancing only when the injected ``sleep`` is awaited."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


# --------------------------------------------------------------------------- #
# Human-lane game seeding (humans-included league; some seats human-occupied)
# --------------------------------------------------------------------------- #


async def _seed_human_lane_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    human_seat_ids: set[str],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed the humans-included league + a claimable human-lane game.

    Human-occupied seats get NO agent build. Returns
    ``(league_id, game_id, agent_builds_by_ai_seat)``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="zai-glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: dict[str, uuid.UUID] = {}
        for i in range(mini7_v1.PLAYER_COUNT):
            seat_id = f"P{i + 1:02d}"
            if seat_id in human_seat_ids:
                continue
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=_RULESET,
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"us146-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds[seat_id] = ab.id

        league = await leagues_repo.get_or_create_humans_included(session, ruleset_id=_RULESET)
        game = await games_repo.create(
            session,
            ruleset_id=_RULESET,
            game_seed=_GAME_SEED,
            status="PENDING",
        )
        # Mark the game LIVE + broadcastable so the live-tail / reveal gating is
        # exercised against the real columns.
        game.broadcast_state = BroadcastState.LIVE.value
        game.is_broadcastable = True
        game.identity_mode = IdentityMode.ANONYMOUS.value
        league_id = league.id
        game_id = game.id
    return league_id, game_id, builds


async def _persist_human_seat_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    human_seat_ids: set[str],
    takeover_seat_id: str | None,
    ai_builds: dict[str, uuid.UUID],
) -> None:
    """Persist GameSeat rows carrying every identity column (HUMAN/AI/AI_TAKEOVER).

    The runner only backfills seats when EVERY seat has an agent build, so a
    mixed human game needs its seat rows written explicitly. These rows carry the
    full identity surface (``seat_kind``, ``occupant_principal_id`` via None,
    ``takeover_agent_build_id``) so the column-level anonymity guard is exercised
    against real data.
    """
    seats = assign_roles(_GAME_SEED, mini7_v1)
    async with session_factory() as session, session.begin():
        for seat in seats:
            sid = seat.public_player_id
            if sid == takeover_seat_id:
                kind = SeatKind.AI_TAKEOVER.value
                takeover_build = next(iter(ai_builds.values()))
            elif sid in human_seat_ids:
                kind = SeatKind.HUMAN.value
                takeover_build = None
            else:
                kind = SeatKind.AI.value
                takeover_build = None
            session.add(
                GameSeat(
                    game_id=game_id,
                    public_player_id=sid,
                    seat_index=seat.seat_index,
                    agent_build_id=ai_builds.get(sid),
                    seat_kind=kind,
                    takeover_agent_build_id=takeover_build,
                    taken_over_at_phase="DAY_1_VOTE"
                    if kind == SeatKind.AI_TAKEOVER.value
                    else None,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )


def _ai_adapter_factory(
    script: Mapping[tuple[str, str], AgentResponse],
) -> AiAdapterFactory:
    def factory(assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        return SeatMultiplexAdapter(
            {seat_id: _ScriptedSeatAdapter(seat_id, script) for seat_id in sorted(assignments)}
        )

    return factory


async def _seed_human_action_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    human_seat_ids: set[str],
    script: Mapping[tuple[str, str], AgentResponse],
) -> None:
    base = datetime(2026, 6, 20, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        for idx, ((phase, seat_id), response) in enumerate(sorted(script.items())):
            if seat_id not in human_seat_ids:
                continue
            session.add(
                HumanActionSubmission(
                    game_id=game_id,
                    public_player_id=seat_id,
                    phase=phase,
                    idempotency_key=f"invariant-{phase}-{seat_id}",
                    action_type=response.action.type.value,
                    target=response.action.target,
                    created_at=base + timedelta(seconds=idx),
                )
            )


async def _drive_human_lane_game_to_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    script: Mapping[tuple[str, str], AgentResponse],
) -> None:
    settings = Settings(
        padrino_human_lane_max_concurrent=1,
        padrino_human_phase_deadline_seconds=1.0,
        padrino_human_release_delay_seconds=0.0,
        padrino_human_reconnect_grace_seconds=60.0,
    )
    ai_factory = _ai_adapter_factory(script)
    executor = human_lane_module._default_human_game_executor(
        settings,
        ai_adapter_factory=ai_factory,
    )
    await human_lane_module._run_one_human_game(
        session_factory,
        game_id=game_id,
        semaphore=asyncio.Semaphore(1),
        adapter_factory=None,
        ai_adapter_factory=ai_factory,
        game_executor=executor,
        settings=settings,
        build_production_adapter=True,
        resume=None,
    )


def _discussion_phase_bodies() -> list[dict[str, object]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    assignments = [
        {
            "public_player_id": seat.public_player_id,
            "seat_index": seat.seat_index,
            "role": seat.role.value,
            "faction": seat.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if seat.public_player_id == _HUMAN_SEAT else SeatKind.AI.value
            ),
        }
        for seat in seats
    ]
    return [
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": mini7_v1.RULESET_ID,
                "game_id": "invariant-chat",
                "game_seed": _GAME_SEED,
                "player_count": mini7_v1.PLAYER_COUNT,
            },
        },
        {
            "event_type": "RolesAssigned",
            "sequence": 1,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"assignments": assignments},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": _DISCUSSION_PHASE,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
        },
    ]


async def _persist_phase_log(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    bodies: list[dict[str, object]],
) -> None:
    log = EventLog()
    for raw in bodies:
        body = dict(raw)
        payload = body["payload"]
        assert isinstance(payload, dict)
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=str(body["event_type"]),
            phase=str(body["phase"]),
            visibility=str(body["visibility"]),
            actor_player_id=body["actor_player_id"]
            if isinstance(body["actor_player_id"], str)
            else None,
            payload=payload,
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


def _discussion_script() -> dict[tuple[str, str], AgentResponse]:
    ai_speaker = next(
        seat.public_player_id
        for seat in assign_roles(_GAME_SEED, mini7_v1)
        if seat.public_player_id != _HUMAN_SEAT
    )
    script: dict[tuple[str, str], AgentResponse] = {}
    for seat in assign_roles(_GAME_SEED, mini7_v1):
        script[(_DISCUSSION_PHASE, seat.public_player_id)] = AgentResponse(
            public_message="AI invariant message" if seat.public_player_id == ai_speaker else None,
            private_message=None,
            action=Action(type=ActionType.NOOP, target=None),
            memory_update="",
            rationale_summary=None,
        )
    return script


async def _seed_discussion_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, dict[tuple[str, str], AgentResponse]]:
    _league_id, game_id, ai_builds = await _seed_human_lane_game(
        session_factory,
        human_seat_ids={_HUMAN_SEAT},
    )
    await _persist_human_seat_rows(
        session_factory,
        game_id=game_id,
        human_seat_ids={_HUMAN_SEAT},
        takeover_seat_id=None,
        ai_builds=ai_builds,
    )
    script = _discussion_script()
    await _seed_human_action_rows(
        session_factory,
        game_id=game_id,
        human_seat_ids={_HUMAN_SEAT},
        script=script,
    )
    async with session_factory() as session, session.begin():
        await _persist_phase_log(session, game_id, bodies=_discussion_phase_bodies())
    return game_id, script


async def _stage_held_public_chat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    text: str,
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            HumanChatSubmission(
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                phase=_DISCUSSION_PHASE,
                channel="PUBLIC",
                idempotency_key="invariant-chat",
                raw_text=text,
                cleaned_text=text,
                status="HELD",
                created_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            )
        )


async def _event_rows(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> list[GameEvent]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _production_callers(symbol: str) -> list[Path]:
    callers: list[Path] = []
    for path in _SRC_ROOT.rglob("*.py"):
        if symbol in _call_names(path):
            callers.append(path.relative_to(_REPO_ROOT))
    return callers


async def _count_rating_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        rating_events = (
            await session.execute(select(func.count()).select_from(RatingEvent))
        ).scalar_one()
        human_ratings = (
            await session.execute(select(func.count()).select_from(HumanRating))
        ).scalar_one()
        human_rating_events = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    return ratings, rating_events, human_ratings, human_rating_events


# --------------------------------------------------------------------------- #
# Fixture: drive ONE real persisted human-lane game to terminal
# --------------------------------------------------------------------------- #


class _PlayedGame:
    def __init__(
        self,
        *,
        game_id: uuid.UUID,
        league_id: uuid.UUID,
        ai_builds: dict[str, uuid.UUID],
        human_seats: set[str],
        takeover_seat: str,
    ) -> None:
        self.game_id = game_id
        self.league_id = league_id
        self.ai_builds = ai_builds
        self.human_seats = human_seats
        self.takeover_seat = takeover_seat


@pytest_asyncio.fixture
async def played_human_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> _PlayedGame:
    """Drive a real mixed human+AI game through the production human lane."""
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    human_seats = {town[0], mafia[0]}
    takeover_seat = town[1]

    league_id, game_id, ai_builds = await _seed_human_lane_game(
        session_factory, human_seat_ids=human_seats
    )
    await _persist_human_seat_rows(
        session_factory,
        game_id=game_id,
        human_seat_ids=human_seats,
        takeover_seat_id=takeover_seat,
        ai_builds=ai_builds,
    )
    await _seed_human_action_rows(
        session_factory,
        game_id=game_id,
        human_seat_ids=human_seats,
        script=script,
    )
    await _drive_human_lane_game_to_terminal(session_factory, game_id=game_id, script=script)

    async with session_factory() as session:
        game = await session.get(Game, game_id)
    assert game is not None
    assert game.status == "COMPLETED"
    assert game.terminal_result == {
        "winner": "TOWN",
        "reason": "ALL_MAFIA_ELIMINATED",
        "day_terminated": 2,
    }

    return _PlayedGame(
        game_id=game_id,
        league_id=league_id,
        ai_builds=ai_builds,
        human_seats=human_seats,
        takeover_seat=takeover_seat,
    )


# --------------------------------------------------------------------------- #
# SEGREGATION gate
# --------------------------------------------------------------------------- #


async def test_segregation_human_game_writes_zero_scientific_rating_rows(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A completed human-lane game writes ZERO rows to ANY rating table."""
    counts = await _count_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# PRODUCTION WIRING gate
# --------------------------------------------------------------------------- #


async def test_wiring_human_lane_executor_uses_human_tick_and_human_adapter() -> None:
    """A human-lane game must not regress to the benchmark runner shortcut."""
    human_lane = _SRC_ROOT / "runner" / "human_lane.py"
    source = human_lane.read_text()
    calls = _call_names(human_lane)

    assert "run_human_tick" in calls
    assert "HumanAdapter" in calls
    assert "run_game" not in calls
    assert "NoopMockAdapter" not in source

    assert _production_callers("run_human_tick")
    assert _production_callers("HumanAdapter")


async def test_wiring_human_and_ai_messages_release_at_same_buffered_instant(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Human chat release uses the same settled instant as AI buffered chat."""
    game_id, script = await _seed_discussion_game(session_factory)
    text = "Human invariant release text"
    await _stage_held_public_chat(session_factory, game_id=game_id, text=text)

    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=Settings(padrino_human_phase_deadline_seconds=_TICK_DEADLINE_S),
        ai_adapter_factory=_ai_adapter_factory(script),
    )
    eligible = [
        seat for seat in state.living_seats() if legal_actions_for(state, seat).allowed_action_types
    ]

    captured: list[HumanTickResult] = []

    async def spy_run_human_tick(
        state: GameState,
        event_log: EventLog,
        eligible_seats: Sequence[Seat],
        adapter: LlmAdapter,
        ruleset: Ruleset,
        config: HumanTickConfig,
        *,
        ranked: bool,
        clock: Clock,
        sleep: Sleep,
    ) -> HumanTickResult:
        result = await _real_run_human_tick(
            state,
            event_log,
            eligible_seats,
            adapter,
            ruleset,
            config,
            ranked=ranked,
            clock=clock,
            sleep=sleep,
        )
        captured.append(result)
        return result

    monkeypatch.setattr(human_lane_module, "run_human_tick", spy_run_human_tick)

    clock = _FakeClock()
    release_base = datetime(2026, 6, 20, 12, tzinfo=UTC)
    human_release_instants: list[float] = []

    async def release_chat(
        phase: str,
        settled_at: float,
        release_log: EventLog,
        pending_lower_events: Sequence[StoredEvent],
    ) -> None:
        human_release_instants.append(settled_at)
        async with session_factory() as session, session.begin():
            await release_held_chat_for_phase(
                session,
                game_id=game_id,
                phase=phase,
                released_at=release_base + timedelta(seconds=settled_at),
                event_log=release_log,
                pending_lower_events=pending_lower_events,
            )

    await human_lane_module._run_human_tick_responses(
        state,
        event_log,
        eligible,
        adapter,
        mini7_v1,
        False,
        _TICK_DEADLINE_S,
        config=HumanTickConfig(
            phase_deadline_seconds=_TICK_DEADLINE_S, release_delay_seconds=2.0
        ),
        clock=clock.now,
        sleep=clock.sleep,
        release_chat=release_chat,
    )

    assert len(captured) == 1
    tick_result = captured[0]
    ai_releases = [m for m in tick_result.released_messages if m.text == "AI invariant message"]
    assert len(ai_releases) == 1
    assert ai_releases[0].released_at == tick_result.settled_at == 2.0
    assert human_release_instants == [tick_result.settled_at]

    async with session_factory() as session:
        hold = (await session.execute(select(HumanChatSubmission))).scalars().one()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().one()

    released_at = hold.released_at
    assert released_at is not None
    if released_at.tzinfo is None:
        released_at = released_at.replace(tzinfo=UTC)
    assert released_at == release_base + timedelta(seconds=tick_result.settled_at)
    assert sidecar.public_player_id == _HUMAN_SEAT
    assert sidecar.raw_text == text

    chained = event_log.events[-1].body
    assert chained["event_type"] == "PublicMessageSubmitted"
    assert chained["actor_player_id"] == _HUMAN_SEAT
    assert chained["payload"] == {
        "text": "",
        "round_index": 1,
        "content_ref": human_chat_content_ref(text),
    }
    assert text not in str(chained)


async def test_wiring_ai_observes_released_human_chat_via_event_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AI observations hydrate released human chat from the sidecar by sequence."""
    game_id, _script = await _seed_discussion_game(session_factory)
    text = "Human invariant text visible to AI"
    await _stage_held_public_chat(session_factory, game_id=game_id, text=text)

    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))
    async with session_factory() as session, session.begin():
        released = await release_held_chat_for_phase(
            session,
            game_id=game_id,
            phase=_DISCUSSION_PHASE,
            released_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            event_log=event_log,
        )
        assert len(released) == 1
        # US-189: release_held_chat_for_phase now co-commits the paired
        # content_ref game_events row in this same transaction.

    capture = _CaptureAdapter()

    def ai_factory(_assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        return capture

    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=Settings(padrino_human_phase_deadline_seconds=_TICK_DEADLINE_S),
        ai_adapter_factory=ai_factory,
    )
    ai_seat = next(seat for seat in state.living_seats() if seat.public_player_id != _HUMAN_SEAT)

    await run_tick(
        state,
        event_log,
        [ai_seat],
        adapter,
        timeout_s=_TICK_DEADLINE_S,
        ruleset=mini7_v1,
        ranked=False,
    )

    assert len(capture.observations) == 1
    observed = capture.observations[0]
    human_messages = [
        entry
        for entry in observed.public_events
        if entry.event_type == "PublicMessageSubmitted" and entry.actor_player_id == _HUMAN_SEAT
    ]
    assert len(human_messages) == 1
    assert human_messages[0].payload["text"] == text
    assert human_messages[0].payload["content_ref"] == human_chat_content_ref(text)

    rows = await _event_rows(session_factory, game_id)
    chained = next(
        row
        for row in rows
        if row.event_type == "PublicMessageSubmitted" and row.actor_player_id == _HUMAN_SEAT
    )
    assert chained.payload["text"] == ""
    assert chained.payload["content_ref"] == human_chat_content_ref(text)
    assert text not in str(chained.payload)


# --------------------------------------------------------------------------- #
# ANONYMITY gate: every pre-reveal read surface is identity-blind
# --------------------------------------------------------------------------- #


async def _raw_events(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> list[dict[str, object]]:
    from padrino.db.models import GameEvent

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )
    return [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in rows
    ]


async def test_anonymity_public_event_v1_projection_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every public_event_v1 frame of the human game leaks zero forbidden keys."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    frames = to_public_events_v1(raw)
    assert frames  # the game produced public frames
    for frame in frames:
        assert_anonymous_safe(frame)
        assert "terminal_result" not in frame


async def test_anonymity_spectator_projection_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The spectator projection of the human game leaks zero forbidden keys."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    for frame in project_events_for_spectator(raw):
        assert_anonymous_safe(frame)


async def test_anonymity_live_tail_stream_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every live-tail SSE frame of the human game leaks zero forbidden keys."""
    cfg = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=5_000)
    frame_count = 0
    async for block in stream_live_tail(session_factory, played_human_game.game_id, config=cfg):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                assert_anonymous_safe(payload)
                frame_count += 1
    assert frame_count  # the live tail actually emitted frames to audit


async def test_anonymity_per_seat_observation_guard_has_no_identity_markers(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No public frame carries a human-vs-AI / model identity marker."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    for frame in to_public_events_v1(raw):
        assert_no_identity_markers(frame)


async def test_anonymity_export_bundle_is_payload_safe(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The export bundle of the completed human game carries no identity leak.

    The export bundle IS the scientific terminal archive: it legitimately
    carries per-seat ``role`` / ``faction`` (that is the whole point of the
    record), so the broad pre-reveal ``assert_anonymous_safe`` does not apply
    here. The export-path invariant is narrower: no model / provider /
    agent-build identifier (``assert_bundle_payload_safe``) and no human-vs-AI /
    model-identity marker (``assert_no_identity_markers``) ever reaches an event
    payload, and the seat-info shape carries NO human-identity column at all.
    """
    async with session_factory() as session:
        bundle = await export_game(session, played_human_game.game_id)
    # assert_bundle_payload_safe raises on any forbidden key; calling it twice
    # (export already calls it internally) documents the invariant explicitly.
    assert_bundle_payload_safe(bundle.events)
    for event in bundle.events:
        assert_no_identity_markers(event.payload)
    # The bundle's per-seat info exposes role/faction (scientific record) but
    # carries ZERO human-identity columns (seat_kind, occupant_*, takeover_*).
    for seat_info in bundle.game_seats:
        fields = set(seat_info.model_dump())
        assert not (HUMAN_IDENTITY_KEYS & fields)


async def test_anonymity_seat_and_game_row_projections_drop_identity_columns(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The column-level guard drops every identity column from real seat/game rows."""
    async with session_factory() as session:
        game = await session.get(Game, played_human_game.game_id)
        assert game is not None
        seats = list(
            (
                await session.execute(
                    select(GameSeat).where(GameSeat.game_id == played_human_game.game_id)
                )
            ).scalars()
        )
    game_row = {c.name: getattr(game, c.name) for c in Game.__table__.columns}
    projected_game = project_game_row(game_row)
    assert "identity_mode" not in projected_game
    assert_anonymous_safe(projected_game)

    for seat in seats:
        seat_row = {c.name: getattr(seat, c.name) for c in GameSeat.__table__.columns}
        # The raw seat row DOES carry identity columns...
        assert HUMAN_IDENTITY_KEYS & set(seat_row)
        projected = project_seat_row(seat_row)
        # ...but the projection drops every one of them.
        assert not (HUMAN_IDENTITY_KEYS & set(projected))
        assert not (IDENTITY_MARKER_KEYS & set(projected))
        assert_anonymous_safe(projected)


# --------------------------------------------------------------------------- #
# REVEAL gate: pre-terminal hides truth; terminal (RECENT) discloses it
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


async def _spectator_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[AsyncClient, str]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        from padrino.db.repositories import api_keys as api_keys_repo

        await api_keys_repo.create(
            session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label="invariant-viewer"
        )
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    return client, raw


async def _human_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _guest(client: AsyncClient) -> tuple[str, uuid.UUID]:
    response = await client.post("/human/guest")
    assert response.status_code == 201, response.text
    return _guest_token(response.headers), uuid.UUID(response.json()["principal_id"])


async def test_reveal_is_hidden_pre_terminal_and_discloses_truth_when_terminal(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The endgame reveal is the ONLY surface that discloses identity, and only
    once the game is terminal (RECENT). While LIVE it 404s; once RECENT it
    reveals which seats were human, roles, models, and takeover provenance."""
    client, raw = await _spectator_client(session_factory)
    headers = {"Authorization": f"Bearer {raw}"}
    try:
        # Pre-terminal (the game is still LIVE): reveal must 404.
        async with session_factory() as session, session.begin():
            game = await session.get(Game, played_human_game.game_id)
            assert game is not None
            game.broadcast_state = BroadcastState.LIVE.value
        r = await client.get(f"/public/games/{played_human_game.game_id}/reveal", headers=headers)
        assert r.status_code == 404

        # Flip to RECENT (broadcast complete) and the reveal opens.
        async with session_factory() as session, session.begin():
            game = await session.get(Game, played_human_game.game_id)
            assert game is not None
            game.broadcast_state = BroadcastState.RECENT.value

        r = await client.get(f"/public/games/{played_human_game.game_id}/reveal", headers=headers)
        assert r.status_code == 200
        body = r.json()
        seats = {s["public_player_id"]: s for s in body["seats"]}

        # Human-held seats reveal as human (decision 11: reveal always discloses).
        for sid in played_human_game.human_seats:
            assert seats[sid]["is_human"] is True
            assert seats[sid]["takeover_provenance"] == "HUMAN"
            assert seats[sid]["model"] is None

        # The taken-over seat reveals HUMAN_THEN_AI provenance + the finishing AI.
        taken = seats[played_human_game.takeover_seat]
        assert taken["is_human"] is False
        assert taken["takeover_provenance"] == "HUMAN_THEN_AI"

        # At least one pure-AI seat reveals its exact model.
        ai_seats = [
            s
            for sid, s in seats.items()
            if sid not in played_human_game.human_seats and sid != played_human_game.takeover_seat
        ]
        assert ai_seats
        assert any(s["model"] is not None for s in ai_seats)
    finally:
        await client.aclose()


async def test_reveal_private_terminal_game_is_reachable_by_participant(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The private human reveal route opens to an authenticated seat occupant."""
    client = await _human_client(session_factory)
    try:
        token, principal_id = await _guest(client)
        participant_seat = next(iter(sorted(played_human_game.human_seats)))
        async with session_factory() as session, session.begin():
            game = await session.get(Game, played_human_game.game_id)
            assert game is not None
            game.broadcast_state = BroadcastState.HIDDEN.value
            game.is_broadcastable = False
            seat = (
                await session.execute(
                    select(GameSeat).where(
                        GameSeat.game_id == played_human_game.game_id,
                        GameSeat.public_player_id == participant_seat,
                    )
                )
            ).scalar_one()
            seat.occupant_principal_id = principal_id

        response = await client.get(
            f"/human/games/{played_human_game.game_id}/reveal",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["game_id"] == str(played_human_game.game_id)
        assert body["winner"] == "TOWN"

        seats = {seat["public_player_id"]: seat for seat in body["seats"]}
        assert seats[participant_seat]["is_human"] is True
        assert seats[participant_seat]["model"] is None
        assert any(seat["model"] is not None for seat in seats.values() if not seat["is_human"])
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Re-run the US-124 / US-125 scaffolds inside this aggregate gate, so a single
# `pytest tests/test_invariants_human_mp.py` enforces both invariants directly.
# --------------------------------------------------------------------------- #


async def test_assert_anonymous_safe_catches_every_human_identity_marker() -> None:
    for key in sorted(HUMAN_IDENTITY_KEYS):
        with pytest.raises(AnonymityViolation):
            assert_anonymous_safe({"seats": [{key: True}]})


async def test_assert_no_identity_markers_catches_model_and_human_markers() -> None:
    for key in sorted(IDENTITY_MARKER_KEYS):
        with pytest.raises(AnonymityViolation):
            assert_no_identity_markers({"meta": {key: "x"}})


async def test_human_invariant_gate_includes_canonical_ruleset_purity() -> None:
    """The aggregate invariant gate also protects the canonical ladder."""
    assert_ruleset_canonical_pure(mini7_v1)
