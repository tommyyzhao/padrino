"""US-158 production wiring for the human-game lane.

The human lane must run through the human-aware tick and DB-backed
``HumanAdapter`` path, not the benchmark ``run_game`` / ``NoopMockAdapter``
shortcut that was acceptable before the POST channels existed.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, Faction, Role, SeatKind
from padrino.core.human_chat import human_chat_content_ref
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatMessage,
    HumanChatSubmission,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
)
from padrino.db.repositories import events as events_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.human_chat_release import release_held_chat_for_phase
from padrino.runner.human_durability import replay_state_from_rows
from padrino.runner.human_lane import (
    AiAdapterFactory,
    _run_human_tick_responses,
    build_human_lane_adapter,
)
from padrino.runner.human_tick import HumanTickConfig, run_human_tick
from padrino.runner.tick import run_tick
from padrino.settings import Settings
from tests.conftest import make_town_win_script

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "padrino"
_GAME_SEED = "us158-human-lane"
_HUMAN_SEAT = "P01"
_DISCUSSION_PHASE = "DAY_1_DISCUSSION_ROUND_1"


class _FakeClock:
    """Monotonic clock that advances only when the injected sleep is awaited."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _ScriptedSeatAdapter:
    """One AI seat's deterministic slice of the shared script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: response for key, response in script.items() if key[1] == seat_id}
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


def test_human_lane_production_wiring_guard() -> None:
    """Guard against regressing the human lane to the benchmark runner path."""
    human_lane = _SRC_ROOT / "runner" / "human_lane.py"
    source = human_lane.read_text()
    calls = _call_names(human_lane)

    assert "run_human_tick" in calls
    assert "HumanAdapter" in calls
    assert "run_game" not in calls
    assert "NoopMockAdapter" not in source

    assert _production_callers("run_human_tick")
    assert _production_callers("HumanAdapter")


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


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


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _consenting_guest(client: AsyncClient) -> str:
    response = await client.post("/human/guest")
    assert response.status_code == 201, response.text
    token = _guest_token(response.headers)
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert consent.status_code == 201, consent.text
    return token


async def _principal_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


async def _seed_agent_build(session: AsyncSession) -> uuid.UUID:
    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    model = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(model)
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"hash-us158-{uuid.uuid4()}",
    )
    session.add(prompt)
    await session.flush()
    build = AgentBuild(
        display_name="cerebras/glm-4.7@us158",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(build)
    await session.flush()
    return build.id


async def _seed_mixed_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    principal_id: uuid.UUID,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        ai_build_id = await _seed_agent_build(session)
        game = Game(
            gauntlet_id=None,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="PENDING",
        )
        session.add(game)
        await session.flush()

        for seat in assign_roles(_GAME_SEED, mini7_v1):
            is_human = seat.public_player_id == _HUMAN_SEAT
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None if is_human else ai_build_id,
                    seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                    occupant_principal_id=principal_id if is_human else None,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
        await session.flush()
        return game.id


def _vote_phase_bodies() -> list[dict[str, object]]:
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
                "game_id": "us158",
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
            "phase": "DAY_1_VOTE",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
        },
    ]


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
                "game_id": "us159",
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


async def _persist_vote_phase_log(session: AsyncSession, game_id: uuid.UUID) -> None:
    await _persist_phase_log(session, game_id, bodies=_vote_phase_bodies())


async def _persist_discussion_phase_log(session: AsyncSession, game_id: uuid.UUID) -> None:
    await _persist_phase_log(session, game_id, bodies=_discussion_phase_bodies())


async def _persist_phase_log(
    session: AsyncSession, game_id: uuid.UUID, *, bodies: list[dict[str, object]]
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
    mafia = [seat.public_player_id for seat in seats if seat.faction == Faction.MAFIA.value]
    town = [seat.public_player_id for seat in seats if seat.faction == Faction.TOWN.value]
    doctor = next(seat.public_player_id for seat in seats if seat.role == Role.DOCTOR.value)
    detective = next(seat.public_player_id for seat in seats if seat.role == Role.DETECTIVE.value)
    return make_town_win_script(
        mafia_ids=mafia,
        town_ids=town,
        doctor_id=doctor,
        detective_id=detective,
    )


def _ai_adapter_factory(
    script: Mapping[tuple[str, str], AgentResponse],
) -> AiAdapterFactory:
    def factory(assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        return SeatMultiplexAdapter(
            {seat_id: _ScriptedSeatAdapter(seat_id, script) for seat_id in sorted(assignments)}
        )

    return factory


def _discussion_script(seats: list[GameSeat]) -> dict[tuple[str, str], AgentResponse]:
    ai_speaker = next(
        seat.public_player_id for seat in seats if seat.public_player_id != _HUMAN_SEAT
    )
    script: dict[tuple[str, str], AgentResponse] = {}
    for seat in seats:
        script[(_DISCUSSION_PHASE, seat.public_player_id)] = AgentResponse(
            public_message="AI message from the same phase"
            if seat.public_player_id == ai_speaker
            else None,
            private_message=None,
            action=Action(type=ActionType.NOOP, target=None),
            memory_update="",
            rationale_summary=None,
        )
    return script


async def _event_rows(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
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


@pytest.mark.asyncio
async def test_human_lane_adapter_consumes_action_submitted_through_post_channel(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    game_id = await _seed_mixed_game(session_factory, principal_id=principal_id)
    seats = await _seat_rows(session_factory, game_id)
    script = _town_win_script_from_seats(seats)
    async with session_factory() as session, session.begin():
        await _persist_vote_phase_log(session, game_id)

    action = script[("DAY_1_VOTE", _HUMAN_SEAT)].action
    body: dict[str, object] = {"type": action.type.value}
    if action.target is not None:
        body["target"] = action.target
    response = await client.post(
        f"/human/games/{game_id}/actions",
        json={"action": body, "idempotency_key": "us158-vote"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        submissions = (
            (
                await session.execute(
                    select(HumanActionSubmission)
                    .where(HumanActionSubmission.game_id == game_id)
                    .where(HumanActionSubmission.public_player_id == _HUMAN_SEAT)
                )
            )
            .scalars()
            .all()
        )
    assert len(submissions) == 1
    assert submissions[0].phase == "DAY_1_VOTE"

    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=Settings(padrino_human_phase_deadline_seconds=0.35),
        ai_adapter_factory=_ai_adapter_factory(script),
    )
    eligible = [
        seat for seat in state.living_seats() if legal_actions_for(state, seat).allowed_action_types
    ]

    result = await run_human_tick(
        state,
        event_log,
        eligible,
        adapter,
        mini7_v1,
        HumanTickConfig(phase_deadline_seconds=0.35, release_delay_seconds=0.0),
        ranked=False,
    )

    assert result.responses[_HUMAN_SEAT].action == action


@pytest.mark.asyncio
async def test_human_lane_releases_posted_chat_on_the_symmetric_tick_schedule(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    game_id = await _seed_mixed_game(session_factory, principal_id=principal_id)
    seats = await _seat_rows(session_factory, game_id)
    script = _discussion_script(seats)
    async with session_factory() as session, session.begin():
        await _persist_discussion_phase_log(session, game_id)

    chat = await client.post(
        f"/human/games/{game_id}/chat",
        json={
            "channel": "PUBLIC",
            "text": "Human message from the same phase",
            "idempotency_key": "us159-chat",
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["status"] == "HELD"

    async with session_factory() as session:
        early_holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        early_sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    assert len(early_holds) == 1
    assert early_holds[0].status == "HELD"
    assert early_sidecar == []

    action = await client.post(
        f"/human/games/{game_id}/actions",
        json={"action": {"type": "NOOP", "target": None}, "idempotency_key": "us159-noop"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert action.status_code == 200, action.text

    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=Settings(padrino_human_phase_deadline_seconds=0.35),
        ai_adapter_factory=_ai_adapter_factory(script),
    )
    eligible = [
        seat for seat in state.living_seats() if legal_actions_for(state, seat).allowed_action_types
    ]
    clock = _FakeClock()
    release_base = datetime(2026, 6, 20, tzinfo=UTC)
    tick_releases: list[float] = []

    async def release_chat(phase: str, settled_at: float, release_log: EventLog) -> None:
        tick_releases.append(settled_at)
        async with session_factory() as session, session.begin():
            await release_held_chat_for_phase(
                session,
                game_id=game_id,
                phase=phase,
                released_at=release_base + timedelta(seconds=settled_at),
                event_log=release_log,
            )

    responses = await _run_human_tick_responses(
        state,
        event_log,
        eligible,
        adapter,
        mini7_v1,
        False,
        0.35,
        config=HumanTickConfig(phase_deadline_seconds=0.35, release_delay_seconds=4.0),
        clock=clock.now,
        sleep=clock.sleep,
        release_chat=release_chat,
    )

    ai_speakers = [seat for seat, response in responses.items() if response.public_message]
    assert len(ai_speakers) == 1
    assert tick_releases == [4.0]

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    assert len(holds) == 1
    assert holds[0].status == "RELEASED"
    released_at = holds[0].released_at
    assert released_at is not None
    if released_at.tzinfo is None:
        released_at = released_at.replace(tzinfo=UTC)
    assert released_at == release_base + timedelta(seconds=4.0)
    assert len(sidecar) == 1
    assert sidecar[0].public_player_id == _HUMAN_SEAT
    assert sidecar[0].raw_text == "Human message from the same phase"
    human_event = event_log.events[-1].body
    assert human_event["event_type"] == "PublicMessageSubmitted"
    assert human_event["actor_player_id"] == _HUMAN_SEAT
    assert human_event["payload"] == {
        "text": "",
        "round_index": 1,
        "content_ref": human_chat_content_ref("Human message from the same phase"),
    }
    assert sidecar[0].sequence == human_event["sequence"]
    assert "Human message from the same phase" not in str(human_event)


@pytest.mark.asyncio
async def test_ai_observation_resolves_released_human_chat_from_sidecar(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _consenting_guest(client)
    principal_id = await _principal_id(session_factory)
    game_id = await _seed_mixed_game(session_factory, principal_id=principal_id)
    async with session_factory() as session, session.begin():
        await _persist_discussion_phase_log(session, game_id)

    chat = await client.post(
        f"/human/games/{game_id}/chat",
        json={
            "channel": "PUBLIC",
            "text": "Sidecar-visible human read for AI",
            "idempotency_key": "us160-chat",
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert chat.status_code == 200, chat.text

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
        stored = event_log.events[-1]
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

    capture = _CaptureAdapter()

    def ai_factory(_assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        return capture

    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=Settings(padrino_human_phase_deadline_seconds=0.35),
        ai_adapter_factory=ai_factory,
    )
    ai_seat = next(seat for seat in state.living_seats() if seat.public_player_id != _HUMAN_SEAT)

    await run_tick(
        state,
        event_log,
        [ai_seat],
        adapter,
        timeout_s=0.35,
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
    assert human_messages[0].payload["text"] == "Sidecar-visible human read for AI"
    assert human_messages[0].payload["content_ref"] == human_chat_content_ref(
        "Sidecar-visible human read for AI"
    )

    async with session_factory() as session:
        rows = await events_repo.list_events(session, game_id)
    chained = next(
        row
        for row in rows
        if row.event_type == "PublicMessageSubmitted" and row.actor_player_id == _HUMAN_SEAT
    )
    assert chained.payload["text"] == ""
    assert chained.payload["content_ref"] == human_chat_content_ref(
        "Sidecar-visible human read for AI"
    )
    assert "Sidecar-visible human read for AI" not in str(chained.payload)
