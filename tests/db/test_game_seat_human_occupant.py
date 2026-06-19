"""US-121: GameSeat human-occupant schema + seat_kind.

Asserts:
- the new ``game_seats`` columns round-trip through the DB;
- a legacy AI-only seat (agent_build_id populated, no Wave 9 fields supplied)
  persists and loads byte-identically with ``seat_kind='AI'``;
- a HUMAN seat may omit ``agent_build_id`` (now nullable);
- the pure core ``Seat`` carries an optional ``seat_kind`` that defaults to None
  so replaying an existing event log reproduces identical state with no change
  to mechanics.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.events import (
    Event,
    GameCreated,
    GameCreatedPayload,
    PhaseStarted,
    PhaseStartedPayload,
    RolesAssigned,
    RolesAssignedPayload,
    SeatAssignment,
)
from padrino.core.engine.replay import replay_events
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, PhaseKind, Role, SeatKind
from padrino.db.models import AgentBuild, Game, GameSeat, ModelConfig, ModelProvider, PromptVersion


async def _make_agent_build(session: AsyncSession) -> AgentBuild:
    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
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
        ruleset_id="mini7_v1",
        version="v1",
        system_prompt="play carefully",
        developer_prompt="reply with JSON",
        response_schema={"type": "object"},
        prompt_hash="hash-us121",
    )
    session.add(pv)
    await session.flush()
    ab = AgentBuild(
        display_name="cerebras/glm-4.7@v1",
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(ab)
    await session.flush()
    return ab


async def _make_game(session: AsyncSession, *, seed: str) -> Game:
    game = Game(gauntlet_id=None, ruleset_id="mini7_v1", game_seed=seed, status="CREATED")
    session.add(game)
    await session.flush()
    return game


async def test_legacy_ai_only_seat_is_byte_identical(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An AI seat written without any Wave 9 fields loads with seat_kind='AI'."""
    async with session_factory() as session:
        ab = await _make_agent_build(session)
        game = await _make_game(session, seed="legacy-seed")
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P01",
                seat_index=0,
                agent_build_id=ab.id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        await session.commit()
        game_id = game.id

    async with session_factory() as session:
        seat = (await session.execute(_select_seat(game_id, "P01"))).scalar_one()
        assert seat.agent_build_id is not None
        assert seat.seat_kind == "AI"
        assert seat.taken_over_at_phase is None
        assert seat.takeover_agent_build_id is None


async def test_human_seat_round_trip_without_agent_build(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A HUMAN seat may persist with a null agent_build_id (column now nullable)."""
    async with session_factory() as session:
        game = await _make_game(session, seed="human-seed")
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P02",
                seat_index=1,
                agent_build_id=None,
                seat_kind="HUMAN",
                role="DETECTIVE",
                faction="TOWN",
                alive=True,
            )
        )
        await session.commit()
        game_id = game.id

    async with session_factory() as session:
        seat = (await session.execute(_select_seat(game_id, "P02"))).scalar_one()
        assert seat.agent_build_id is None
        assert seat.seat_kind == "HUMAN"


async def test_ai_takeover_seat_records_provenance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An AI_TAKEOVER seat round-trips its takeover provenance columns."""
    async with session_factory() as session:
        ab = await _make_agent_build(session)
        game = await _make_game(session, seed="takeover-seed")
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P03",
                seat_index=2,
                agent_build_id=None,
                seat_kind="AI_TAKEOVER",
                role="VILLAGER",
                faction="TOWN",
                alive=True,
                taken_over_at_phase="DAY_VOTE:2",
                takeover_agent_build_id=ab.id,
            )
        )
        await session.commit()
        game_id = game.id
        ab_id = ab.id

    async with session_factory() as session:
        seat = (await session.execute(_select_seat(game_id, "P03"))).scalar_one()
        assert seat.seat_kind == "AI_TAKEOVER"
        assert seat.taken_over_at_phase == "DAY_VOTE:2"
        assert seat.takeover_agent_build_id == ab_id


def test_core_seat_seat_kind_is_optional_and_defaults_none() -> None:
    seat = Seat(
        public_player_id="P01",
        seat_index=0,
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        alive=True,
    )
    assert seat.seat_kind is None
    human = seat.model_copy(update={"seat_kind": SeatKind.HUMAN})
    assert human.seat_kind is SeatKind.HUMAN
    # The field is pure data: nothing else about the seat changed.
    assert human.model_copy(update={"seat_kind": None}) == seat


def test_legacy_event_log_replays_to_identical_state() -> None:
    """Replaying a pre-Wave-9 log yields seats with seat_kind=None (unchanged)."""
    assignments = (
        SeatAssignment(
            public_player_id="P01", seat_index=0, role=Role.MAFIA_GOON, faction=Faction.MAFIA
        ),
        SeatAssignment(
            public_player_id="P02", seat_index=1, role=Role.DETECTIVE, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P03", seat_index=2, role=Role.VILLAGER, faction=Faction.TOWN
        ),
    )
    events: list[Event] = [
        GameCreated(
            sequence=0,
            phase="SETUP",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1", game_id="G1", game_seed="seed-legacy", player_count=3
            ),
        ),
        RolesAssigned(
            sequence=1, phase="SETUP", payload=RolesAssignedPayload(assignments=assignments)
        ),
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION",
            payload=PhaseStartedPayload(phase_kind=PhaseKind.DAY_DISCUSSION.value, day=1, round=1),
        ),
    ]
    state = replay_events(events)
    assert len(state.seats) == 3
    for seat in state.seats:
        assert seat.seat_kind is None
    # Replaying the identical log a second time is byte-stable.
    assert replay_events(events) == state


def _select_seat(game_id: uuid.UUID, public_player_id: str) -> Select[tuple[GameSeat]]:
    return select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.public_player_id == public_player_id,
    )
