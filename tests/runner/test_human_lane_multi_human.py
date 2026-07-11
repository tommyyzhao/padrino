"""US-331 coverage for production multi-human lane behavior."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, SeatKind
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatSubmission,
    ModelConfig,
    ModelProvider,
    Principal,
    PromptVersion,
)
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as LlmAgentBuild
from padrino.llm.human_adapter import HumanAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.human_durability import rehydrate_active_human_games, replay_state_from_rows
from padrino.runner.human_lane import (
    _take_over_expired_human_seats,
    build_human_lane_adapter,
    persist_human_runtime_snapshot,
)
from padrino.settings import Settings

_GAME_SEED = "us331-multi-human"
_HUMAN_SEATS = ("P03", "P04")
_VOTE_PHASE = "DAY_1_VOTE"
_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
_DEADLINE = datetime(2026, 7, 11, 12, 5, tzinfo=UTC)


class _NoopAiAdapter:
    async def complete(self, observation: Observation) -> AdapterResult:
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


async def _seed_principals(session: AsyncSession, count: int) -> list[uuid.UUID]:
    principals = [Principal(kind="guest") for _ in range(count)]
    session.add_all(principals)
    await session.flush()
    return [principal.id for principal in principals]


async def _seed_agent_build(session: AsyncSession, *, label: str) -> uuid.UUID:
    provider = ModelProvider(name=f"us331-provider-{label}", auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    model = ModelConfig(
        provider_id=provider.id,
        model_name=f"zai-glm-4.7-{label}",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"us331-{label}-{uuid.uuid4()}",
    )
    session.add_all([model, prompt])
    await session.flush()
    build = AgentBuild(
        display_name=f"us331-{label}",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="2026.07",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(build)
    await session.flush()
    return build.id


def _vote_phase_bodies(game_id: uuid.UUID) -> list[dict[str, object]]:
    assignments = [
        {
            "public_player_id": seat.public_player_id,
            "seat_index": seat.seat_index,
            "role": seat.role.value,
            "faction": seat.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if seat.public_player_id in _HUMAN_SEATS else SeatKind.AI.value
            ),
        }
        for seat in assign_roles(_GAME_SEED, mini7_v1)
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
                "game_id": str(game_id),
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
            "phase": _VOTE_PHASE,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
        },
    ]


async def _persist_vote_phase(session: AsyncSession, game_id: uuid.UUID) -> EventLog:
    log = EventLog()
    for body in _vote_phase_bodies(game_id):
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
    return log


async def _seed_multi_human_vote_game(
    session: AsyncSession,
    *,
    status: str = "RUNNING",
) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    principal_ids = await _seed_principals(session, len(_HUMAN_SEATS))
    ai_build_id = await _seed_agent_build(session, label=f"ai-{uuid.uuid4()}")
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status=status,
    )
    session.add(game)
    await session.flush()
    await _persist_vote_phase(session, game.id)

    principal_by_seat = dict(zip(_HUMAN_SEATS, principal_ids, strict=True))
    for seat in assign_roles(_GAME_SEED, mini7_v1):
        is_human = seat.public_player_id in _HUMAN_SEATS
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None if is_human else ai_build_id,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                occupant_principal_id=principal_by_seat.get(seat.public_player_id),
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
            )
        )
    await session.flush()
    return game.id, principal_ids, ai_build_id


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


def _settings() -> Settings:
    return Settings(
        padrino_human_phase_deadline_seconds=0.05,
        padrino_human_reconnect_grace_seconds=90.0,
        padrino_human_global_lobby_cost_breaker_usd=10_000.0,
    )


@pytest.mark.asyncio
async def test_build_adapter_creates_distinct_human_adapters_and_all_ai_assignments(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, _principal_ids, _ai_build_id = await _seed_multi_human_vote_game(session)

    captured_ai_assignments: dict[str, LlmAgentBuild] = {}

    def ai_factory(assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        captured_ai_assignments.update(assignments)
        return _NoopAiAdapter()

    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=ai_factory,
    )

    assert isinstance(adapter, SeatMultiplexAdapter)
    seat_adapters = adapter._adapters
    all_seats = {seat.public_player_id for seat in assign_roles(_GAME_SEED, mini7_v1)}
    ai_seats = all_seats.difference(_HUMAN_SEATS)

    assert set(seat_adapters) == all_seats
    assert isinstance(seat_adapters["P03"], HumanAdapter)
    assert isinstance(seat_adapters["P04"], HumanAdapter)
    assert seat_adapters["P03"] is not seat_adapters["P04"]
    assert set(captured_ai_assignments) == ai_seats
    assert all(not isinstance(seat_adapters[seat_id], HumanAdapter) for seat_id in ai_seats)


@pytest.mark.asyncio
async def test_presence_expiry_takes_over_only_the_disconnected_human_seat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, _principal_ids, _ai_build_id = await _seed_multi_human_vote_game(session)
        await _seed_agent_build(session, label=f"takeover-{uuid.uuid4()}")
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id="P03",
            disconnected_at=_NOW - timedelta(seconds=180),
        )
        await presence_repo.record_heartbeat(
            session,
            game_id=game_id,
            public_player_id="P04",
            seen_at=_NOW,
        )

    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=lambda _assignments: _NoopAiAdapter(),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)
    assert isinstance(adapter._adapters["P03"], HumanAdapter)
    assert isinstance(adapter._adapters["P04"], HumanAdapter)
    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))

    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=lambda _assignments: _NoopAiAdapter(),
        now=_NOW,
    )

    assert [item.seat_id for item in applied] == ["P03"]
    assert not isinstance(adapter._adapters["P03"], HumanAdapter)
    assert isinstance(adapter._adapters["P04"], HumanAdapter)

    async with session_factory() as session:
        seats = {
            row.public_player_id: row
            for row in (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .where(GameSeat.public_player_id.in_(_HUMAN_SEATS))
                )
            ).scalars()
        }
    assert seats["P03"].seat_kind == SeatKind.AI_TAKEOVER.value
    assert seats["P04"].seat_kind == SeatKind.HUMAN.value


@pytest.mark.asyncio
async def test_restart_rehydrates_multiple_buffered_human_inputs_and_hash_head(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game_id, _principal_ids, _ai_build_id = await _seed_multi_human_vote_game(session)
        session.add_all(
            [
                HumanActionSubmission(
                    game_id=game_id,
                    public_player_id="P03",
                    phase=_VOTE_PHASE,
                    idempotency_key="p03-old",
                    action_type=ActionType.VOTE.value,
                    target="P05",
                    created_at=_NOW,
                ),
                HumanActionSubmission(
                    game_id=game_id,
                    public_player_id="P03",
                    phase=_VOTE_PHASE,
                    idempotency_key="p03-new",
                    action_type=ActionType.ABSTAIN.value,
                    target=None,
                    created_at=_NOW + timedelta(seconds=1),
                ),
                HumanActionSubmission(
                    game_id=game_id,
                    public_player_id="P04",
                    phase=_VOTE_PHASE,
                    idempotency_key="p04",
                    action_type=ActionType.VOTE.value,
                    target="P03",
                    created_at=_NOW,
                ),
                HumanChatSubmission(
                    game_id=game_id,
                    public_player_id="P03",
                    phase=_VOTE_PHASE,
                    channel="PUBLIC",
                    idempotency_key="chat-p03",
                    raw_text="p03 held chat",
                    cleaned_text="p03 held chat",
                    status="HELD",
                    created_at=_NOW,
                ),
                HumanChatSubmission(
                    game_id=game_id,
                    public_player_id="P04",
                    phase=_VOTE_PHASE,
                    channel="PUBLIC",
                    idempotency_key="chat-p04",
                    raw_text="p04 held chat",
                    cleaned_text=None,
                    status="HELD",
                    created_at=_NOW,
                ),
            ]
        )

    state, event_log = replay_state_from_rows(await _event_rows(session_factory, game_id))
    await persist_human_runtime_snapshot(
        session_factory,
        game_id=game_id,
        phase=_VOTE_PHASE,
        deadline_at=_DEADLINE,
        updated_at=_NOW,
        state=state,
        event_log=event_log,
    )

    rehydrated = await rehydrate_active_human_games(session_factory)

    assert len(rehydrated) == 1
    resumed = rehydrated[0]
    assert resumed.game_id == game_id
    assert resumed.deadline_at == _DEADLINE
    assert resumed.event_log.head_hash == event_log.head_hash
    assert resumed.buffer_snapshot["actions"] == {
        "P03": {
            "action_type": "ABSTAIN",
            "target": None,
            "idempotency_key": "p03-new",
            "created_at": (_NOW + timedelta(seconds=1)).isoformat(),
        },
        "P04": {
            "action_type": "VOTE",
            "target": "P03",
            "idempotency_key": "p04",
            "created_at": _NOW.isoformat(),
        },
    }
    assert resumed.buffer_snapshot["chat_holds"] == [
        {
            "public_player_id": "P03",
            "channel": "PUBLIC",
            "status": "HELD",
            "idempotency_key": "chat-p03",
            "created_at": _NOW.isoformat(),
            "ready_for_release": True,
        },
        {
            "public_player_id": "P04",
            "channel": "PUBLIC",
            "status": "HELD",
            "idempotency_key": "chat-p04",
            "created_at": _NOW.isoformat(),
            "ready_for_release": False,
        },
    ]

    async with session_factory() as session:
        game = await session.get(Game, game_id)
    assert game is not None
    assert game.event_hash_head == event_log.head_hash
