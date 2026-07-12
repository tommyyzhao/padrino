"""End-to-end human-game coverage for real multi-human lobbies.

These default-suite guards exercise the human spine through REAL HTTP channels,
so a cross-boundary contract break can never pass a green suite (the Wave-7
lesson). The scenarios walk:

    create lobby -> guests join -> consent -> ready -> lock -> launch (curated
    auto-fill materializes real human-lane Game + GameSeat rows) -> the
    production human worker lane drives the game while scripted human clients
    POST independent structured actions and buffered chat over authenticated
    channels -> the game reaches terminal -> reveal opens identity -> replay
    verifies the persisted hash chain.

Human seats are NOT shortcut: production :class:`HumanAdapter` instances poll
the DB-backed POST action store exactly like production. AI seats are driven by
a deterministic mock script. Tests use short injected settings and never touch
a real LLM.

Two invariants are asserted across the whole spine (the Wave-9 non-negotiables):

* ANONYMITY held on every live frame (no forbidden human-vs-AI / model keys),
  and the reveal is the ONLY surface that opens identity (a LIVE game 404s).
* ZERO rows were written to the scientific ``ratings`` / ``rating_events``
  tables (and zero to the dormant human-rating siblings, casual v1).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.api.human_observation import DEADLINE_FRAME, OBSERVATION_FRAME
from padrino.core.agents.contract import AgentResponse
from padrino.core.cadence import CASUAL_FAST_CADENCE_ID, cadence_profile
from padrino.core.engine.event_log import StoredEvent
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.replay import replay_event_log, replay_events
from padrino.core.enums import ActionType, Faction, Role, SeatKind
from padrino.core.human_chat import human_chat_content_ref
from padrino.core.observation_privacy import assert_anonymous_safe
from padrino.core.observations import Observation, format_phase_id
from padrino.core.rulesets.snapshot import GameRulesSnapshot, canonical_rules_hash
from padrino.db.models import (
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatMessage,
    HumanPlayerStats,
    HumanRating,
    HumanRatingEvent,
    Lobby,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.public.broadcast_index import BroadcastState
from padrino.public.live_tail import LiveTailConfig, stream_live_tail
from padrino.runner.human_durability import rehydrate_active_human_games, replay_state_from_rows
from padrino.runner.human_lane import _default_human_game_executor, _run_one_human_game
from padrino.runner.human_state_cache import resolve_current_human_state
from padrino.settings import Settings
from tests.conftest import (
    ScriptedAction,
    make_role_aware_script,
    make_town_win_script,
    phase_ids_for_ruleset,
)

_RULESET = "mini7_v1"
_PLAYER_COUNT = 7
_ROSTER_MEMBER_KEYS = {"member_id", "display_label", "is_self", "host", "ready", "present"}


class _ScriptedSeatAdapter:
    """An LLM adapter returning one AI seat's slice of a phase-keyed script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: resp for key, resp in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


# --------------------------------------------------------------------------- #
# HTTP helpers (guest auth, consent, lobby spine)
# --------------------------------------------------------------------------- #


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _new_guest(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201, resp.text
    return _guest_token(resp.headers)


async def _consent(client: AsyncClient, token: str) -> None:
    resp = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 201, resp.text


async def _seed_curated_pool(
    session: AsyncSession,
    *,
    count: int,
    ruleset_id: str,
) -> None:
    """Seed ``count`` active curated builds targeting one ruleset."""
    from padrino.db.models import AgentBuild, ModelConfig, ModelProvider, PromptVersion

    suffix = uuid.uuid4().hex
    provider = ModelProvider(
        name=f"cerebras-{suffix}", base_url=None, auth_secret_ref="CEREBRAS_API_KEY"
    )
    session.add(provider)
    await session.flush()
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(mc)
    pv = PromptVersion(
        ruleset_id=ruleset_id,
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"hash-us332-{suffix}",
    )
    session.add(pv)
    await session.flush()
    for i in range(count):
        session.add(
            AgentBuild(
                display_name=f"cerebras/glm-4.7@v1-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
        )
    await session.commit()


@dataclass(frozen=True, slots=True)
class _JoinedHuman:
    token: str
    principal_id: uuid.UUID
    public_player_id: str


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest.fixture(autouse=True)
def _pristine_settings() -> Iterator[None]:
    """Use pristine default settings so lobby admission caps are deterministic.

    Other suites pin tiny caps / breaker thresholds via env + ``get_settings``
    cache flips; clearing the cache before and after this test keeps the
    cumulative-cost admission gate from inheriting a leaked low cap that would
    deny lobby creation only when this test runs after them in the full suite.
    """
    from padrino.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# --------------------------------------------------------------------------- #
# Game-driving glue: launch real lobbies + drive production HumanAdapters
# --------------------------------------------------------------------------- #


async def _seat_rows(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> list[GameSeat]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )


def _town_win_script_from_seats(seats: list[GameSeat]) -> dict[tuple[str, str], AgentResponse]:
    """Build a TOWN-win script from the actually-assigned seat roles.

    The lobby (and hence game) seed is random, so roles are only known after the
    launch handoff materializes the seat rows. The script makes whichever seats
    hold each role follow the canonical town-win plan; the host seat follows its
    own assigned role's scripted actions just like an AI would.
    """
    mafia = [s.public_player_id for s in seats if s.faction == Faction.MAFIA.value]
    town = [s.public_player_id for s in seats if s.faction == Faction.TOWN.value]
    doctor = next(s.public_player_id for s in seats if s.role == Role.DOCTOR.value)
    detective = next(s.public_player_id for s in seats if s.role == Role.DETECTIVE.value)
    return make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )


def _custom_tracker_watcher_counts() -> dict[str, int]:
    return {
        Role.MAFIA_GOON.value: 2,
        Role.DETECTIVE.value: 1,
        Role.DOCTOR.value: 1,
        Role.TRACKER.value: 1,
        Role.WATCHER.value: 1,
        Role.VILLAGER.value: 1,
    }


@dataclass(frozen=True, slots=True)
class _CustomNarPlan:
    tracker: str
    watcher: str
    tracked_mafia: str
    watched_target: str


def _custom_tracker_watcher_script_from_seats(
    seats: list[GameSeat],
    rules_snapshot: GameRulesSnapshot,
) -> tuple[dict[tuple[str, str], AgentResponse], _CustomNarPlan]:
    """Drive a custom seven-seat deck through tracker/watcher NAR effects."""
    mafia = [s.public_player_id for s in seats if s.faction == Faction.MAFIA.value]
    town = [s.public_player_id for s in seats if s.faction == Faction.TOWN.value]
    doctor = next(s.public_player_id for s in seats if s.role == Role.DOCTOR.value)
    detective = next(s.public_player_id for s in seats if s.role == Role.DETECTIVE.value)
    tracker = next(s.public_player_id for s in seats if s.role == Role.TRACKER.value)
    watcher = next(s.public_player_id for s in seats if s.role == Role.WATCHER.value)
    if len(mafia) != 2:
        raise AssertionError("custom tracker/watcher proof expects exactly two mafia")

    watched_target = next(sid for sid in town if sid not in {doctor, tracker, watcher})
    actions: dict[tuple[str, str], ScriptedAction] = {}
    for sid in town:
        actions[("DAY_1_VOTE", sid)] = ScriptedAction(ActionType.VOTE, mafia[0])
        actions[("DAY_2_VOTE", sid)] = ScriptedAction(ActionType.VOTE, mafia[1])
    for mid in mafia:
        actions[("NIGHT_1_ACTIONS", mid)] = ScriptedAction(
            ActionType.MAFIA_KILL,
            watched_target,
        )
    actions[("NIGHT_1_ACTIONS", doctor)] = ScriptedAction(ActionType.PROTECT, watched_target)
    actions[("NIGHT_1_ACTIONS", detective)] = ScriptedAction(
        ActionType.INVESTIGATE,
        mafia[1],
    )
    actions[("NIGHT_1_ACTIONS", tracker)] = ScriptedAction(ActionType.TRACK, mafia[1])
    actions[("NIGHT_1_ACTIONS", watcher)] = ScriptedAction(ActionType.WATCH, watched_target)

    script = make_role_aware_script(
        [s.public_player_id for s in seats],
        phase_ids_for_ruleset(rules_snapshot),
        actions=actions,
    )
    return script, _CustomNarPlan(
        tracker=tracker,
        watcher=watcher,
        tracked_mafia=mafia[1],
        watched_target=watched_target,
    )


def _ai_adapter_factory(
    script: Mapping[tuple[str, str], AgentResponse],
) -> LlmAdapter:
    return SeatMultiplexAdapter(
        {seat_id: _ScriptedSeatAdapter(seat_id, script) for _phase, seat_id in script}
    )


def _human_lane_settings() -> Settings:
    return Settings(
        padrino_human_phase_deadline_seconds=5.0,
        padrino_human_release_delay_seconds=0.0,
        padrino_human_reconnect_grace_seconds=300.0,
        padrino_human_global_lobby_cost_breaker_usd=10_000.0,
    )


async def _new_consented_guest(client: AsyncClient) -> tuple[str, uuid.UUID]:
    token = await _new_guest(client)
    principal_id = uuid.UUID(
        (await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: token})).json()[
            "principal_id"
        ]
    )
    await _consent(client, token)
    return token, principal_id


async def _launch_lobby(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    human_count: int,
    lobby_body: Mapping[str, object] | None = None,
    player_count: int = _PLAYER_COUNT,
) -> tuple[uuid.UUID, list[_JoinedHuman], list[GameSeat]]:
    host_token, host_principal_id = await _new_consented_guest(client)
    created = await client.post(
        "/lobbies",
        json=dict(lobby_body or {"ruleset_id": _RULESET}),
        cookies={HUMAN_SESSION_COOKIE: host_token},
    )
    assert created.status_code == 201, created.text
    lobby = created.json()
    lobby_id = uuid.UUID(lobby["id"])
    invite_token = lobby["invite_token"]
    assert "seats" not in lobby
    assert lobby["composition"]["total"] == player_count

    async with session_factory() as session:
        await _seed_curated_pool(session, count=player_count, ruleset_id=lobby["ruleset_id"])

    sessions: list[tuple[str, uuid.UUID]] = [(host_token, host_principal_id)]
    for _ in range(human_count - 1):
        token, principal_id = await _new_consented_guest(client)
        joined = await client.post(
            f"/lobbies/join/{invite_token}",
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert joined.status_code == 200, joined.text
        sessions.append((token, principal_id))

    roster = await client.get(
        f"/lobbies/{lobby_id}/roster", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert roster.status_code == 200, roster.text
    assert roster.json()["member_count"] == human_count
    for member in roster.json()["members"]:
        assert set(member) == _ROSTER_MEMBER_KEYS

    for token, _principal_id in sessions:
        ready = await client.post(
            f"/lobbies/{lobby_id}/ready",
            json={"ready": True},
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        assert ready.status_code == 200, ready.text

    locked = await client.post(
        f"/lobbies/{lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert locked.status_code == 200, locked.text
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["created"] is True
    game_id = uuid.UUID(launched.json()["game_id"])

    seats = await _seat_rows(session_factory, game_id)
    human_seats = [s for s in seats if s.seat_kind == SeatKind.HUMAN.value]
    assert len(human_seats) == human_count
    by_principal = {s.occupant_principal_id: s for s in human_seats}
    assert set(by_principal) == {principal_id for _token, principal_id in sessions}
    humans = [
        _JoinedHuman(
            token=token,
            principal_id=principal_id,
            public_player_id=by_principal[principal_id].public_player_id,
        )
        for token, principal_id in sessions
    ]
    return game_id, humans, seats


def _parse_sse_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                frames.append(json.loads(payload))
    return frames


async def _assert_human_observation_access(
    client: AsyncClient,
    game_id: uuid.UUID,
    humans: Sequence[_JoinedHuman],
) -> None:
    for human in humans:
        resp = await client.get(
            f"/human/games/{game_id}/observation/stream",
            cookies={HUMAN_SESSION_COOKIE: human.token},
        )
        assert resp.status_code == 200, resp.text
        frames = _parse_sse_frames(resp.text)
        obs = next(frame for frame in frames if frame["type"] == OBSERVATION_FRAME)
        deadline = next(frame for frame in frames if frame["type"] == DEADLINE_FRAME)
        assert obs["you"]["player_id"] == human.public_player_id
        assert deadline["phase"] == obs["phase"]
        rendered = json.dumps(frames)
        assert "is_human" not in rendered
        assert "model_name" not in rendered
        assert "agent_build_id" not in rendered

    stranger = await _new_guest(client)
    denied = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: stranger},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "wrong_seat"


async def _wait_for_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        async with session_factory() as session:
            runtime = await runtime_repo.get(session, game_id)
            if runtime is not None:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"human runtime row was not created for {game_id}")


async def _scripted_human_submitter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    human: _JoinedHuman,
    script: Mapping[tuple[str, str], AgentResponse],
    stop: asyncio.Event,
    chat_text: str,
) -> None:
    attempted_actions: set[str] = set()
    chat_sent = False
    while not stop.is_set():
        async with session_factory() as session:
            game = await session.get(Game, game_id)
            if game is not None and game.status == "COMPLETED":
                return
            resolved = await resolve_current_human_state(session, game_id)
            if resolved is None:
                phase = None
                action_allowed = False
                public_chat_allowed = False
            else:
                state = resolved.state
                phase = format_phase_id(state.current_phase)
                seat = state.seat_by_public_id(human.public_player_id)
                if seat is None or not seat.alive:
                    action_allowed = False
                    public_chat_allowed = False
                else:
                    legal = legal_actions_for(state, seat)
                    scripted = script.get((phase, human.public_player_id))
                    action_allowed = (
                        scripted is not None
                        and scripted.action.type in legal.allowed_action_types
                        and (
                            scripted.action.target is None
                            or scripted.action.target in legal.legal_targets
                        )
                    )
                    public_chat_allowed = "_DISCUSSION_" in phase

        if phase is not None and action_allowed and phase not in attempted_actions:
            scripted = script[(phase, human.public_player_id)]
            body: dict[str, object] = {"type": scripted.action.type.value}
            if scripted.action.target is not None:
                body["target"] = scripted.action.target
            resp = await client.post(
                f"/human/games/{game_id}/actions",
                json={
                    "action": body,
                    "idempotency_key": f"act-{human.public_player_id}-{phase}",
                },
                cookies={HUMAN_SESSION_COOKIE: human.token},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["public_player_id"] == human.public_player_id
            attempted_actions.add(phase)

        if phase is not None and public_chat_allowed and not chat_sent:
            chat = await client.post(
                f"/human/games/{game_id}/chat",
                json={
                    "channel": "PUBLIC",
                    "text": chat_text,
                    "idempotency_key": f"chat-{human.public_player_id}",
                },
                cookies={HUMAN_SESSION_COOKIE: human.token},
            )
            assert chat.status_code == 200, chat.text
            assert chat.json()["public_player_id"] == human.public_player_id
            assert chat.json()["status"] == "HELD"
            chat_sent = True

        await asyncio.sleep(0.01)


async def _run_human_lane_game(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    humans: Sequence[_JoinedHuman],
    script: Mapping[tuple[str, str], AgentResponse],
    disconnected: set[str] | None = None,
    after_runtime_started: Callable[[], Awaitable[None]] | None = None,
) -> None:
    settings = _human_lane_settings()
    disconnected_ids = disconnected or set()
    if disconnected_ids:
        async with session_factory() as session, session.begin():
            for public_player_id in disconnected_ids:
                await presence_repo.mark_disconnected(
                    session,
                    game_id=game_id,
                    public_player_id=public_player_id,
                    disconnected_at=datetime.now(UTC) - timedelta(seconds=600),
                )

    def ai_factory(assignments: Mapping[str, object]) -> LlmAdapter:
        return SeatMultiplexAdapter(
            {seat_id: _ScriptedSeatAdapter(seat_id, script) for seat_id in assignments}
        )

    executor = _default_human_game_executor(settings, ai_adapter_factory=ai_factory)
    stop = asyncio.Event()
    runner = asyncio.create_task(
        _run_one_human_game(
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
    )
    await _wait_for_runtime(session_factory, game_id)
    if after_runtime_started is not None:
        await after_runtime_started()
    await _assert_human_observation_access(
        client,
        game_id,
        [human for human in humans if human.public_player_id not in disconnected_ids],
    )

    submitters = [
        asyncio.create_task(
            _scripted_human_submitter(
                client,
                session_factory,
                game_id=game_id,
                human=human,
                script=script,
                stop=stop,
                chat_text=f"{human.public_player_id} is checking the table.",
            )
        )
        for human in humans
        if human.public_player_id not in disconnected_ids
    ]
    try:
        await asyncio.wait_for(runner, timeout=30.0)
    finally:
        stop.set()
        if submitters:
            await asyncio.gather(*submitters, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Shared assertions
# --------------------------------------------------------------------------- #


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


async def _assert_live_frames_anonymous(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> None:
    """Every live-tail SSE frame of the human game leaks zero forbidden keys."""
    cfg = LiveTailConfig(poll_ms=25, heartbeat_ms=1_000_000, idle_timeout_ms=5_000)
    frame_count = 0
    async for block in stream_live_tail(session_factory, game_id, config=cfg):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                assert_anonymous_safe(payload)
                frame_count += 1
    assert frame_count  # the live tail actually emitted frames to audit


async def _mark_live(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> None:
    async with session_factory() as session, session.begin():
        game = await session.get(Game, game_id)
        assert game is not None
        game.broadcast_state = BroadcastState.LIVE.value
        game.is_broadcastable = True


async def _read_events(
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


def _stored_events_from_rows(rows: Sequence[GameEvent]) -> list[StoredEvent]:
    return [
        StoredEvent(
            sequence=row.sequence,
            prev_event_hash=row.prev_event_hash,
            event_hash=row.event_hash,
            body={
                "event_type": row.event_type,
                "sequence": row.sequence,
                "phase": row.phase,
                "visibility": row.visibility,
                "actor_player_id": row.actor_player_id,
                "payload": row.payload,
            },
        )
        for row in rows
    ]


async def _lobby_game_event_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with session_factory() as session:
        lobbies = (await session.execute(select(func.count()).select_from(Lobby))).scalar_one()
        games = (await session.execute(select(func.count()).select_from(Game))).scalar_one()
        events = (await session.execute(select(func.count()).select_from(GameEvent))).scalar_one()
    return lobbies, games, events


async def _rules_snapshot_for_game(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> tuple[GameRulesSnapshot, str]:
    async with session_factory() as session:
        game = await session.get(Game, game_id)
    assert game is not None
    assert game.rules_snapshot_json is not None
    assert game.rules_snapshot_hash is not None
    snapshot = GameRulesSnapshot.model_validate(game.rules_snapshot_json)
    assert canonical_rules_hash(snapshot) == game.rules_snapshot_hash
    return snapshot, game.rules_snapshot_hash


async def _assert_custom_nar_effects(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    plan: _CustomNarPlan,
) -> None:
    rows = await _read_events(session_factory, game_id)
    by_type: dict[str, list[GameEvent]] = {}
    for row in rows:
        by_type.setdefault(row.event_type, []).append(row)

    assert any(
        row.actor_player_id == plan.tracker and row.payload.get("target") == plan.tracked_mafia
        for row in by_type.get("TrackSubmitted", [])
    )
    assert any(
        row.actor_player_id == plan.watcher and row.payload.get("target") == plan.watched_target
        for row in by_type.get("WatchSubmitted", [])
    )

    feedback = by_type.get("NightFeedbackDelivered", [])
    assert any(
        row.actor_player_id == plan.tracker
        and row.payload.get("code") == "TRACK_RESULT"
        and row.payload.get("target") == plan.tracked_mafia
        for row in feedback
    ), [(row.actor_player_id, row.payload) for row in feedback]
    assert any(
        row.actor_player_id == plan.watcher
        and row.payload.get("code") == "WATCH_RESULT"
        and row.payload.get("target") == plan.watched_target
        for row in feedback
    ), [(row.actor_player_id, row.payload) for row in feedback]


async def _assert_custom_replay_without_live_ruleset(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    rules_snapshot: GameRulesSnapshot,
) -> None:
    rows = await _read_events(session_factory, game_id)
    stored = _stored_events_from_rows(rows)
    log = replay_event_log(stored, rules_snapshot=rules_snapshot)
    typed_events = [EventAdapter.validate_python(event.body) for event in log.events]
    final = replay_events(typed_events, rules_snapshot=rules_snapshot)
    assert final.terminal_result == "TOWN"
    assert log.events[-1].event_hash == rows[-1].event_hash


async def _assert_fast_cadence_and_rehydration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    rules_snapshot: GameRulesSnapshot,
) -> None:
    profile = cadence_profile(CASUAL_FAST_CADENCE_ID)
    async with session_factory() as session:
        runtime = await runtime_repo.get(session, game_id)
    assert runtime is not None
    assert runtime.deadline_at is not None
    assert rules_snapshot.cadence_id == CASUAL_FAST_CADENCE_ID

    rehydrated = await rehydrate_active_human_games(session_factory)
    match = next(item for item in rehydrated if item.game_id == game_id)
    assert match.phase == runtime.phase
    assert match.deadline_at is not None
    assert match.deadline_at.replace(tzinfo=None) == runtime.deadline_at.replace(tzinfo=None)
    assert match.event_log.head_hash
    timing = profile.timing_for(
        match.state.current_phase.kind,
        round_number=match.state.current_phase.round,
    )
    remaining = (runtime.deadline_at - runtime.updated_at).total_seconds()
    assert remaining == pytest.approx(timing.deadline_seconds, abs=0.25)


async def _assert_personal_stats_only(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    humans: Sequence[_JoinedHuman],
    ruleset_id: str,
) -> None:
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HumanPlayerStats).where(
                        HumanPlayerStats.principal_id.in_([human.principal_id for human in humans])
                    )
                )
            ).scalars()
        )
    assert {row.principal_id for row in rows} == {human.principal_id for human in humans}
    assert {row.ruleset_id for row in rows} == {ruleset_id}
    assert all(row.games == 1 for row in rows)
    assert await _count_rating_rows(session_factory) == (0, 0, 0, 0)


async def _assert_terminal_and_replay(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    auth: Mapping[str, str],
) -> None:
    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        assert game.status == "COMPLETED"
        assert isinstance(game.terminal_result, dict)
        assert game.terminal_result["winner"] == "TOWN"
        event_hash_head = game.event_hash_head

    rows = await _read_events(session_factory, game_id)
    replayed_state, replayed_log = replay_state_from_rows(rows)
    assert replayed_state.terminal_result == "TOWN"
    assert replayed_log.head_hash == event_hash_head
    assert [event.event_hash for event in replayed_log.events] == [row.event_hash for row in rows]
    assert [event.body["sequence"] for event in replayed_log.events] == [
        row.sequence for row in rows
    ]

    response = await client.post(f"/games/{game_id}/replay", headers=dict(auth))
    assert response.status_code == 200, response.text
    assert response.json()["replay_status"] == "PASS"
    assert response.json()["final_event_hash"] == event_hash_head


async def _assert_sidecar_chat_only(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    expected_texts_by_seat: Mapping[str, str],
) -> None:
    async with session_factory() as session:
        sidecar = list(
            (
                await session.execute(
                    select(HumanChatMessage)
                    .where(HumanChatMessage.game_id == game_id)
                    .order_by(HumanChatMessage.public_player_id)
                )
            ).scalars()
        )
    assert {row.public_player_id: row.raw_text for row in sidecar} == dict(expected_texts_by_seat)

    for row in sidecar:
        assert row.raw_text is not None
        async with session_factory() as session:
            chained = (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .where(GameEvent.sequence == row.sequence)
                )
            ).scalar_one()
        assert chained.event_type == "PublicMessageSubmitted"
        assert chained.actor_player_id == row.public_player_id
        assert chained.payload["text"] == ""
        assert chained.payload["content_ref"] == human_chat_content_ref(row.raw_text)
        assert row.raw_text not in json.dumps(chained.payload)


async def _assert_action_submissions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    expected_human_seats: Sequence[str],
    absent_human_seats: Sequence[str] = (),
) -> None:
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HumanActionSubmission).where(HumanActionSubmission.game_id == game_id)
                )
            ).scalars()
        )
    by_seat: dict[str, list[HumanActionSubmission]] = {}
    for row in rows:
        by_seat.setdefault(row.public_player_id, []).append(row)
    for public_player_id in expected_human_seats:
        assert by_seat[public_player_id]
        assert all(
            row.action_type in {t.value for t in ActionType} for row in by_seat[public_player_id]
        )
    for public_player_id in absent_human_seats:
        assert public_player_id not in by_seat


async def _spectator_auth(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> dict[str, str]:
    raw_key = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw_key, scopes=[SCOPE_SPECTATOR], label=label)
    return {"Authorization": f"Bearer {raw_key}"}


async def _assert_reveal(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    auth: Mapping[str, str],
    human_seats: Sequence[str],
    takeover_seats: Sequence[str] = (),
) -> dict[str, dict[str, object]]:
    pre = await client.get(f"/public/games/{game_id}/reveal", headers=dict(auth))
    assert pre.status_code == 404

    async with session_factory() as session, session.begin():
        game = await session.get(Game, game_id)
        assert game is not None
        game.broadcast_state = BroadcastState.RECENT.value

    revealed = await client.get(f"/public/games/{game_id}/reveal", headers=dict(auth))
    assert revealed.status_code == 200, revealed.text
    body = revealed.json()
    assert body["winner"] == "TOWN"
    reveal_seats = {s["public_player_id"]: s for s in body["seats"]}

    for public_player_id in human_seats:
        seat = reveal_seats[public_player_id]
        assert seat["is_human"] is True
        assert seat["takeover_provenance"] == "HUMAN"
        assert seat["model"] is None

    for public_player_id in takeover_seats:
        seat = reveal_seats[public_player_id]
        assert seat["is_human"] is False
        assert seat["takeover_provenance"] == "HUMAN_THEN_AI"
        assert seat["taken_over_at_phase"] is not None
        assert seat["model"] is not None

    ai_reveals = [
        seat
        for public_player_id, seat in reveal_seats.items()
        if public_player_id not in set(human_seats).union(takeover_seats)
    ]
    assert len(ai_reveals) == _PLAYER_COUNT - len(human_seats) - len(takeover_seats)
    assert all(seat["is_human"] is False for seat in ai_reveals)
    assert all(seat["model"] is not None for seat in ai_reveals)
    assert all(seat["model"]["model_name"] == "zai-glm-4.7" for seat in ai_reveals)
    return reveal_seats


@pytest.mark.asyncio
async def test_custom_rules_cadence_and_segregation_end_to_end(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_host = await _new_guest(client)
    before_counts = await _lobby_game_event_counts(session_factory)
    invalid = await client.post(
        "/lobbies",
        json={
            "custom_role_counts": {Role.MAFIA_GOON.value: 4, Role.VILLAGER.value: 3},
            "human_seat_count": 2,
            "cadence_preset": CASUAL_FAST_CADENCE_ID,
        },
        cookies={HUMAN_SESSION_COOKIE: invalid_host},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["validation_errors"] == ["invalid_faction_ratio"]
    assert await _lobby_game_event_counts(session_factory) == before_counts

    game_id, humans, seats = await _launch_lobby(
        client,
        session_factory,
        human_count=2,
        lobby_body={
            "custom_role_counts": _custom_tracker_watcher_counts(),
            "human_seat_count": 2,
            "cadence_preset": CASUAL_FAST_CADENCE_ID,
        },
    )
    rules_snapshot, rules_snapshot_hash = await _rules_snapshot_for_game(
        session_factory,
        game_id,
    )
    assert f"custom:{rules_snapshot_hash}" == rules_snapshot.RULESET_ID
    assert rules_snapshot.cadence_id == CASUAL_FAST_CADENCE_ID
    assert {Role.TRACKER, Role.WATCHER}.issubset(rules_snapshot.ROLE_COUNTS)

    script, nar_plan = _custom_tracker_watcher_script_from_seats(seats, rules_snapshot)
    cadence_rehydrated = False

    async def assert_runtime_started() -> None:
        nonlocal cadence_rehydrated
        await _assert_fast_cadence_and_rehydration(
            session_factory,
            game_id=game_id,
            rules_snapshot=rules_snapshot,
        )
        cadence_rehydrated = True

    await _mark_live(session_factory, game_id)
    await _run_human_lane_game(
        client,
        session_factory,
        game_id=game_id,
        humans=humans,
        script=script,
        after_runtime_started=assert_runtime_started,
    )
    assert cadence_rehydrated

    auth = await _spectator_auth(session_factory, label="us339-custom-rules")
    await _assert_terminal_and_replay(client, session_factory, game_id=game_id, auth=auth)
    await _assert_custom_nar_effects(session_factory, game_id=game_id, plan=nar_plan)
    await _assert_sidecar_chat_only(
        session_factory,
        game_id=game_id,
        expected_texts_by_seat={
            human.public_player_id: f"{human.public_player_id} is checking the table."
            for human in humans
        },
    )
    await _assert_live_frames_anonymous(session_factory, game_id)
    await _assert_personal_stats_only(
        session_factory,
        humans=humans,
        ruleset_id=rules_snapshot.RULESET_ID,
    )

    def fail_live_ruleset_resolution(ruleset_id: str) -> object:
        raise AssertionError(f"live ruleset resolution disabled for {ruleset_id}")

    monkeypatch.setattr("padrino.core.rulesets.get_ruleset", fail_live_ruleset_resolution)
    monkeypatch.setattr("padrino.runner.game_runner.get_ruleset", fail_live_ruleset_resolution)
    await _assert_custom_replay_without_live_ruleset(
        session_factory,
        game_id=game_id,
        rules_snapshot=rules_snapshot,
    )

    await _assert_reveal(
        client,
        session_factory,
        game_id=game_id,
        auth=auth,
        human_seats=[human.public_player_id for human in humans],
    )


@pytest.mark.asyncio
async def test_two_human_mini7_game_completes_through_real_http_and_replay(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, humans, seats = await _launch_lobby(client, session_factory, human_count=2)
    assert all(human.public_player_id for human in humans)
    await _mark_live(session_factory, game_id)
    script = _town_win_script_from_seats(seats)
    await _run_human_lane_game(
        client, session_factory, game_id=game_id, humans=humans, script=script
    )

    auth = await _spectator_auth(session_factory, label="us332-two-human")
    await _assert_terminal_and_replay(client, session_factory, game_id=game_id, auth=auth)
    await _assert_sidecar_chat_only(
        session_factory,
        game_id=game_id,
        expected_texts_by_seat={
            human.public_player_id: f"{human.public_player_id} is checking the table."
            for human in humans
        },
    )
    await _assert_action_submissions(
        session_factory,
        game_id=game_id,
        expected_human_seats=[human.public_player_id for human in humans],
    )
    await _assert_live_frames_anonymous(session_factory, game_id)
    assert await _count_rating_rows(session_factory) == (0, 0, 0, 0)
    reveal_seats = await _assert_reveal(
        client,
        session_factory,
        game_id=game_id,
        auth=auth,
        human_seats=[human.public_player_id for human in humans],
    )

    host = humans[0]
    guess = {
        sid: ("HUMAN" if sid in {human.public_player_id for human in humans[1:]} else "AI")
        for sid in reveal_seats
        if sid != host.public_player_id
    }
    guess_resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": guess},
        cookies={HUMAN_SESSION_COOKIE: host.token},
    )
    assert guess_resp.status_code == 200, guess_resp.text
    score = guess_resp.json()
    assert score["guesser_public_id"] == host.public_player_id
    assert score["total"] == _PLAYER_COUNT - 1
    assert score["correct"] == _PLAYER_COUNT - 1
    assert score["idempotent_replay"] is False

    own = await client.get(
        f"/human/games/{game_id}/turing-guess", cookies={HUMAN_SESSION_COOKIE: host.token}
    )
    assert own.status_code == 200
    assert own.json()["correct"] == _PLAYER_COUNT - 1


@pytest.mark.asyncio
async def test_three_human_game_finishes_after_one_disconnect_takeover(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, humans, seats = await _launch_lobby(client, session_factory, human_count=3)
    disconnected = humans[-1]
    connected = humans[:-1]
    await _mark_live(session_factory, game_id)
    script = _town_win_script_from_seats(seats)

    await _run_human_lane_game(
        client,
        session_factory,
        game_id=game_id,
        humans=humans,
        script=script,
        disconnected={disconnected.public_player_id},
    )

    auth = await _spectator_auth(session_factory, label="us332-three-human")
    await _assert_terminal_and_replay(client, session_factory, game_id=game_id, auth=auth)
    await _assert_sidecar_chat_only(
        session_factory,
        game_id=game_id,
        expected_texts_by_seat={
            human.public_player_id: f"{human.public_player_id} is checking the table."
            for human in connected
        },
    )
    await _assert_action_submissions(
        session_factory,
        game_id=game_id,
        expected_human_seats=[human.public_player_id for human in connected],
        absent_human_seats=[disconnected.public_player_id],
    )
    await _assert_live_frames_anonymous(session_factory, game_id)
    assert await _count_rating_rows(session_factory) == (0, 0, 0, 0)

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == disconnected.public_player_id,
                )
            )
        ).scalar_one()
    assert seat.seat_kind == SeatKind.AI_TAKEOVER.value
    assert seat.occupant_principal_id == disconnected.principal_id
    assert seat.takeover_agent_build_id is not None
    assert seat.taken_over_at_phase is not None

    await _assert_reveal(
        client,
        session_factory,
        game_id=game_id,
        auth=auth,
        human_seats=[human.public_player_id for human in connected],
        takeover_seats=[disconnected.public_player_id],
    )
