"""US-162: disconnect grace is wired into the production human lane."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import padrino.runner.human_lane as human_lane
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.reducer import compute_seat_provenance
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState, Phase
from padrino.core.enums import ActionType, Faction, PhaseKind, Role, SeatKind
from padrino.core.observations import Observation
from padrino.core.reveal import (
    PROVENANCE_HUMAN_THEN_AI,
    RevealModel,
    SeatRevealInput,
    project_endgame_reveal,
)
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
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
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.runner.disconnect_takeover import build_takeover_event
from padrino.runner.human_durability import replay_state_from_rows
from padrino.runner.human_lane import (
    AiAdapterFactory,
    TakeoverApplyRecoveryError,
    _apply_committed_takeover,
    _default_human_game_executor,
    _run_human_tick_responses,
    _take_over_expired_human_seats,
    build_human_lane_adapter,
)
from padrino.runner.human_tick import HumanTickConfig
from padrino.settings import Settings
from tests.conftest import make_town_win_script

_GAME_SEED = "us162-human-lane-takeover"
_HUMAN_SEAT = "P01"
_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


class _ScriptedSeatAdapter:
    """One seat's deterministic slice of a shared script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: response for key, response in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


async def _seed_principal(session: AsyncSession) -> uuid.UUID:
    principal = Principal(kind="guest")
    session.add(principal)
    await session.flush()
    return principal.id


async def _seed_agent_build(session: AsyncSession, *, label: str) -> uuid.UUID:
    provider = ModelProvider(name=f"cerebras-{label}", auth_secret_ref="CEREBRAS_API_KEY")
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
    session.add(model)
    prompt = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"us162-{label}-{uuid.uuid4()}",
    )
    session.add(prompt)
    await session.flush()
    build = AgentBuild(
        display_name=f"human-takeover-{label}",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="2026.06",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(build)
    await session.flush()
    return build.id


async def _seed_game(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    ai_build_id: uuid.UUID,
    status: str = "PENDING",
) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status=status,
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


def _script_for_game() -> dict[tuple[str, str], AgentResponse]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return make_town_win_script(
        mafia_ids=mafia,
        town_ids=town,
        doctor_id=doctor,
        detective_id=detective,
    )


def _ai_adapter_factory(script: Mapping[tuple[str, str], AgentResponse]) -> AiAdapterFactory:
    def factory(assignments: Mapping[str, LlmAgentBuild]) -> LlmAdapter:
        return SeatMultiplexAdapter(
            {seat_id: _ScriptedSeatAdapter(seat_id, script) for seat_id in assignments}
        )

    return factory


def _settings() -> Settings:
    return Settings(
        padrino_human_phase_deadline_seconds=0.02,
        padrino_human_release_delay_seconds=0.0,
        padrino_human_reconnect_grace_seconds=90.0,
        padrino_human_global_lobby_cost_breaker_usd=10_000.0,
    )


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


async def test_worker_lane_persists_takeover_and_flips_seat_kind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="ai")
        takeover_build_id = await _seed_agent_build(session, label="takeover")
        game_id = await _seed_game(session, principal_id=principal_id, ai_build_id=ai_build_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=datetime.now(UTC) - timedelta(seconds=180),
        )

    settings = _settings()
    script = _script_for_game()
    executor = _default_human_game_executor(
        settings,
        ai_adapter_factory=_ai_adapter_factory(script),
    )
    await human_lane._run_one_human_game(
        session_factory,
        game_id=game_id,
        semaphore=asyncio.Semaphore(1),
        adapter_factory=None,
        ai_adapter_factory=_ai_adapter_factory(script),
        game_executor=executor,
        settings=settings,
        build_production_adapter=True,
        resume=None,
    )

    rows = await _event_rows(session_factory, game_id)
    takeover_rows = [row for row in rows if row.event_type == "SeatTakenOver"]
    assert len(takeover_rows) == 1
    takeover = takeover_rows[0]
    assert takeover.payload["public_player_id"] == _HUMAN_SEAT
    assert takeover.payload["reason"] == "disconnect_grace_expired"
    assert rows[takeover.sequence - 1].event_type == "PhaseStarted"
    first_resolved = next(row.sequence for row in rows if row.event_type == "PhaseResolved")
    assert takeover.sequence < first_resolved

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == _HUMAN_SEAT,
                )
            )
        ).scalar_one()

    assert seat.seat_kind == SeatKind.AI_TAKEOVER.value
    assert seat.occupant_principal_id == principal_id
    assert seat.takeover_agent_build_id in {ai_build_id, takeover_build_id}
    assert seat.taken_over_at_phase == takeover.phase

    reveal = project_endgame_reveal(
        game_id=str(game_id),
        ruleset_id=mini7_v1.RULESET_ID,
        winner="TOWN",
        seats=[
            SeatRevealInput(
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                seat_kind=seat.seat_kind,
                role=seat.role,
                faction=seat.faction,
                alive=seat.alive,
                taken_over_at_phase=seat.taken_over_at_phase,
                model=RevealModel(
                    provider="cerebras",
                    model_name="zai-glm-4.7",
                    agent_build_id=str(seat.takeover_agent_build_id),
                ),
            )
        ],
    )
    assert reveal.seats[0].takeover_provenance == PROVENANCE_HUMAN_THEN_AI


def _vote_phase_bodies(game_id: uuid.UUID) -> list[dict[str, Any]]:
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
            "phase": "DAY_1_VOTE",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
        },
    ]


async def _persist_vote_phase(session: AsyncSession, game_id: uuid.UUID) -> None:
    log = EventLog()
    for body in _vote_phase_bodies(game_id):
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=body["event_type"],
            phase=body["phase"],
            visibility=body["visibility"],
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


async def test_reconnect_race_cancels_takeover_and_keeps_human_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="race-ai")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )
        session.add(
            HumanActionSubmission(
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                phase="DAY_1_VOTE",
                idempotency_key="race-vote",
                action_type=ActionType.ABSTAIN.value,
                target=None,
                created_at=_NOW,
            )
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)

    async def reconnect_before_recheck() -> None:
        async with session_factory() as session, session.begin():
            await presence_repo.record_heartbeat(
                session,
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                seen_at=_NOW,
            )

    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
        before_revalidate=reconnect_before_recheck,
    )

    assert applied == []
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 0

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == _HUMAN_SEAT,
                )
            )
        ).scalar_one()
    assert seat.seat_kind == SeatKind.HUMAN.value

    human_state_seat = state.seat_by_public_id(_HUMAN_SEAT)
    assert human_state_seat is not None
    assert legal_actions_for(state, human_state_seat).allowed_action_types
    responses = await _run_human_tick_responses(
        state,
        event_log,
        [human_state_seat],
        adapter,
        mini7_v1,
        False,
        0.02,
        config=HumanTickConfig(phase_deadline_seconds=0.02, release_delay_seconds=0.0),
    )
    assert responses[_HUMAN_SEAT].action == Action(type=ActionType.ABSTAIN, target=None)


async def test_reconnect_in_revalidation_read_window_cancels_takeover(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US-200(b): a reconnect committing in the revalidation read->commit window.

    Unlike test_reconnect_race_cancels_takeover_and_keeps_human_turn (which
    reconnects BEFORE revalidation), this one fires the reconnect inside
    ``_expired_human_seat_for_update`` -- between the seat FOR UPDATE lock and the
    presence read -- to model a heartbeat that commits AFTER the worker started
    the takeover transaction. The FOR UPDATE presence read must observe the
    fresher heartbeat and ``seats_past_grace`` re-evaluation must cancel the
    takeover, leaving the human's seat and turn intact.
    """
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="window-ai")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )
        session.add(
            HumanActionSubmission(
                game_id=game_id,
                public_player_id=_HUMAN_SEAT,
                phase="DAY_1_VOTE",
                idempotency_key="window-vote",
                action_type=ActionType.ABSTAIN.value,
                target=None,
                created_at=_NOW,
            )
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)

    real_get = presence_repo.get
    fired = {"done": False}

    async def get_with_reconnect_in_window(
        session: AsyncSession,
        *,
        game_id: uuid.UUID,
        public_player_id: str,
        for_update: bool = False,
    ) -> Any:
        # Only the locking read inside revalidation passes for_update=True; fire
        # the reconnect heartbeat (committed in its own session) ONCE, right
        # before the locked read observes presence -- modelling a heartbeat that
        # commits in the read->commit window.
        if for_update and not fired["done"]:
            fired["done"] = True
            async with session_factory() as beat, beat.begin():
                await presence_repo.record_heartbeat(
                    beat,
                    game_id=game_id,
                    public_player_id=public_player_id,
                    seen_at=_NOW,
                )
        return await real_get(
            session,
            game_id=game_id,
            public_player_id=public_player_id,
            for_update=for_update,
        )

    # human_lane imports this same module object, so patching it here is what the
    # revalidation read inside _expired_human_seat_for_update will resolve.
    monkeypatch.setattr(presence_repo, "get", get_with_reconnect_in_window)

    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )

    assert fired["done"] is True
    assert applied == []
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 0

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == _HUMAN_SEAT,
                )
            )
        ).scalar_one()
    assert seat.seat_kind == SeatKind.HUMAN.value

    human_state_seat = state.seat_by_public_id(_HUMAN_SEAT)
    assert human_state_seat is not None
    assert legal_actions_for(state, human_state_seat).allowed_action_types


async def test_takeover_recheck_is_idempotent_after_seat_flips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="idempotent-ai")
        await _seed_agent_build(session, label="idempotent-takeover")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)

    first = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )
    second = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )

    assert [result.seat_id for result in first] == [_HUMAN_SEAT]
    assert second == []
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 1


async def test_takeover_event_row_is_co_committed_with_seat_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """US-189: the SeatTakenOver row lands atomically with the seat_kind flip.

    Simulate an interrupted run: drive only the takeover (the seat mutation +
    paired event), WITHOUT the outer game loop's persist_pending_events, then
    rehydrate from game_events alone and assert the reconstructed state still
    carries the takeover provenance (no AI_TAKEOVER seat missing its event).
    """
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="atomic-ai")
        await _seed_agent_build(session, label="atomic-takeover")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)

    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )
    assert [result.seat_id for result in applied] == [_HUMAN_SEAT]
    takeover_sequence = applied[0].event.sequence

    # The committed seat mutation has a matching game_events row at the expected
    # sequence even though persist_pending_events never ran (crash window).
    persisted = await _event_rows(session_factory, game_id)
    takeover_rows = [row for row in persisted if row.event_type == "SeatTakenOver"]
    assert len(takeover_rows) == 1
    assert takeover_rows[0].sequence == takeover_sequence
    assert takeover_rows[0].payload["public_player_id"] == _HUMAN_SEAT

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == _HUMAN_SEAT,
                )
            )
        ).scalar_one()
    assert seat.seat_kind == SeatKind.AI_TAKEOVER.value

    # Rehydrate from game_events ONLY: the takeover provenance is reconstructable
    # (no AI_TAKEOVER seat is left without its SeatTakenOver event).
    _rehydrated_state, rehydrated_log = replay_state_from_rows(
        await _event_rows(session_factory, game_id)
    )
    decoded = [EventAdapter.validate_python(e.body) for e in rehydrated_log.events]
    provenance = compute_seat_provenance(decoded)
    assert provenance[_HUMAN_SEAT] == PROVENANCE_HUMAN_THEN_AI


async def test_takeover_commit_failure_leaves_in_memory_uncorrupted(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US-197: a mid-takeover commit failure must not corrupt in-memory state.

    Force the takeover transaction to raise (patched ``_persist_stored_event_row``,
    standing in for any flush/commit failure inside ``session.begin()``) and
    assert (a) the in-memory mux still routes the seat to the HUMAN adapter and
    the event_log has NO orphaned SeatTakenOver, and (b) a subsequent tick does
    not append a second SeatTakenOver nor re-persist a rolled-back sequence.
    """
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="failwin-ai")
        await _seed_agent_build(session, label="failwin-takeover")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    sequences_before = [e.sequence for e in event_log.events]
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)
    assert isinstance(adapter._adapters[_HUMAN_SEAT], HumanAdapter)

    boom = RuntimeError("simulated takeover transaction failure")

    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise boom

    monkeypatch.setattr(human_lane, "_persist_stored_event_row", _raise)

    with pytest.raises(RuntimeError):
        await _take_over_expired_human_seats(
            session_factory,
            game_id=game_id,
            mux=adapter,
            event_log=event_log,
            state=state,
            settings=_settings(),
            ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
            now=_NOW,
        )

    # (a) The in-memory swap NEVER advanced past the durable DB state: the seat
    # still routes to the HUMAN adapter and the log holds no orphaned event.
    assert isinstance(adapter._adapters[_HUMAN_SEAT], HumanAdapter)
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 0
    assert [e.sequence for e in event_log.events] == sequences_before

    # The DB rolled back: no SeatTakenOver row, seat is still HUMAN/expired.
    persisted = await _event_rows(session_factory, game_id)
    assert [r for r in persisted if r.event_type == "SeatTakenOver"] == []
    async with session_factory() as session:
        seat = (
            await session.execute(
                select(GameSeat).where(
                    GameSeat.game_id == game_id,
                    GameSeat.public_player_id == _HUMAN_SEAT,
                )
            )
        ).scalar_one()
    assert seat.seat_kind == SeatKind.HUMAN.value

    # (b) A subsequent tick (commit now succeeds) takes over EXACTLY once: one
    # SeatTakenOver in-memory, one persisted row, one contiguous new sequence.
    monkeypatch.undo()
    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )
    assert [result.seat_id for result in applied] == [_HUMAN_SEAT]
    assert not isinstance(adapter._adapters[_HUMAN_SEAT], HumanAdapter)
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 1
    assert [e.sequence for e in event_log.events] == [*sequences_before, sequences_before[-1] + 1]

    persisted_after = await _event_rows(session_factory, game_id)
    takeover_rows = [r for r in persisted_after if r.event_type == "SeatTakenOver"]
    assert len(takeover_rows) == 1
    assert takeover_rows[0].sequence == sequences_before[-1] + 1


async def test_takeover_apply_failure_resyncs_committed_event(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US-206: a post-commit apply failure must not leave DB ahead of memory."""
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(session)
        ai_build_id = await _seed_agent_build(session, label="failapply-ai")
        await _seed_agent_build(session, label="failapply-takeover")
        game_id = await _seed_game(
            session,
            principal_id=principal_id,
            ai_build_id=ai_build_id,
            status="RUNNING",
        )
        await _persist_vote_phase(session, game_id)
        await presence_repo.mark_disconnected(
            session,
            game_id=game_id,
            public_player_id=_HUMAN_SEAT,
            disconnected_at=_NOW - timedelta(seconds=180),
        )

    rows = await _event_rows(session_factory, game_id)
    state, event_log = replay_state_from_rows(rows)
    sequences_before = [e.sequence for e in event_log.events]
    adapter = await build_human_lane_adapter(
        session_factory,
        game_id=game_id,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
    )
    assert isinstance(adapter, SeatMultiplexAdapter)
    assert isinstance(adapter._adapters[_HUMAN_SEAT], HumanAdapter)

    boom = RuntimeError("simulated post-commit apply failure")

    def _raise_once(*_args: Any, **_kwargs: Any) -> Any:
        monkeypatch.undo()
        raise boom

    monkeypatch.setattr(human_lane, "apply_takeover", _raise_once)

    applied = await _take_over_expired_human_seats(
        session_factory,
        game_id=game_id,
        mux=adapter,
        event_log=event_log,
        state=state,
        settings=_settings(),
        ai_adapter_factory=_ai_adapter_factory(_script_for_game()),
        now=_NOW,
    )

    assert [result.seat_id for result in applied] == [_HUMAN_SEAT]
    assert not isinstance(adapter._adapters[_HUMAN_SEAT], HumanAdapter)
    assert [e.body["event_type"] for e in event_log.events].count("SeatTakenOver") == 1
    assert [e.sequence for e in event_log.events] == [*sequences_before, sequences_before[-1] + 1]

    persisted = await _event_rows(session_factory, game_id)
    takeover_rows = [r for r in persisted if r.event_type == "SeatTakenOver"]
    assert len(takeover_rows) == 1
    assert takeover_rows[0].sequence == event_log.events[-1].sequence
    assert takeover_rows[0].event_hash == event_log.events[-1].event_hash

    next_body = {
        "event_type": "PhaseResolved",
        "sequence": len(event_log.events),
        "phase": "DAY_1_VOTE",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
    }
    next_event = event_log.append(next_body)
    async with session_factory() as session, session.begin():
        await human_lane._persist_stored_event_row(session, game_id=game_id, stored=next_event)

    persisted_after = await _event_rows(session_factory, game_id)
    assert [row.sequence for row in persisted_after] == [
        *sequences_before,
        next_event.sequence - 1,
        next_event.sequence,
    ]


def test_takeover_apply_recovery_surfaces_unknown_mux_seat() -> None:
    """US-211: recovery must not fabricate routing for an unknown seat desync."""
    event_log = EventLog()
    state = GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-UNKNOWN-TAKEOVER",
        game_seed="unknown-seat-recovery",
        current_phase=Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        seats=(),
        day=1,
    )
    event = build_takeover_event(
        event_log=event_log,
        state=state,
        seat_id="P99",
        replacement_agent_build_ref="curated-autofill",
    )
    mux = SeatMultiplexAdapter({"P01": _ScriptedSeatAdapter("P01", {})})
    known_seats_before = set(mux._adapters)

    with pytest.raises(TakeoverApplyRecoveryError, match="unknown seat"):
        _apply_committed_takeover(
            mux=mux,
            event_log=event_log,
            event=event,
            seat_id="P99",
            replacement_adapter=_ScriptedSeatAdapter("P99", {}),
        )

    assert set(mux._adapters) == known_seats_before
    assert "P99" not in mux._adapters
    assert event_log.events == ()
