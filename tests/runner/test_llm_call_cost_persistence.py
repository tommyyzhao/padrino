"""US-232: token-price fallback for persisted LLM call costs."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import GameSeat
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.economics.human_cost_governance import global_human_lane_spend_usd
from padrino.llm.adapter import AdapterResult, AdapterStatus
from padrino.runner.game_runner import GamePersistence, _persist_llm_call
from padrino.settings import Settings


def _observation() -> Observation:
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id="G-COST-PERSIST",
        phase="DAY_1_DISCUSSION_ROUND_1",
        day=1,
        round=1,
        you=YouInfo(
            player_id="P01",
            alive=True,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
        ),
        alive_players=("P01",),
        dead_players=(),
        public_events=(),
        private_events=(),
        legal_actions=LegalActions(allowed_action_types=[ActionType.NOOP], legal_targets=[]),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=500,
            private_message_max_chars=500,
            memory_update_max_chars=500,
        ),
    )


def _result(
    *,
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None = None,
    status: AdapterStatus = "ok",
    raw_response: str | None = None,
) -> AdapterResult:
    response = AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=ActionType.NOOP, target=None),
        memory_update="",
        rationale_summary=None,
    )
    return AdapterResult(
        raw_response=response.model_dump_json() if raw_response is None else raw_response,
        parsed_response=response,
        latency_ms=12,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        model_id=model_id,
        status=status,
    )


async def _game_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        game = await games_repo.create(
            session,
            ruleset_id="mini7_v1",
            game_seed=f"seed-cost-{uuid.uuid4()}",
            status="RUNNING",
        )
        return game.id


async def _persist_result(
    session_factory: async_sessionmaker[AsyncSession],
    result: AdapterResult,
    *,
    settings: Settings,
    human_seat: bool = False,
) -> tuple[uuid.UUID, float | None]:
    game_id = await _game_id(session_factory)
    if human_seat:
        async with session_factory() as session, session.begin():
            session.add(
                GameSeat(
                    game_id=game_id,
                    public_player_id="P01",
                    seat_index=0,
                    seat_kind="HUMAN",
                    role=Role.VILLAGER.value,
                    faction=Faction.TOWN.value,
                    alive=True,
                )
            )

    await _persist_llm_call(
        GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            settings=settings,
        ),
        _observation(),
        result,
    )

    async with session_factory() as session:
        rows = await llm_calls_repo.list_for_game(session, game_id)
    assert len(rows) == 1
    persisted_cost = rows[0].cost_usd
    return game_id, float(persisted_cost) if persisted_cost is not None else None


async def test_persisted_llm_call_uses_token_price_when_response_cost_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )

    _, cost = await _persist_result(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1234, output_tokens=250),
        settings=settings,
    )

    assert cost is not None
    assert cost == pytest.approx(0.003968)
    assert cost > 0.0


async def test_persisted_llm_call_prefers_litellm_response_cost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (10.0, 10.0),
        },
    )

    _, cost = await _persist_result(
        session_factory,
        _result(
            model_id="openai/glm-4.7",
            input_tokens=10_000,
            output_tokens=10_000,
            cost_usd=0.42,
        ),
        settings=settings,
    )

    assert cost is not None
    assert cost == pytest.approx(0.42)


async def test_persisted_llm_call_uses_configured_model_price(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "custom/provider-model": (0.01, 0.02),
        },
    )

    _, cost = await _persist_result(
        session_factory,
        _result(model_id="custom/provider-model", input_tokens=1000, output_tokens=1000),
        settings=settings,
    )

    assert cost is not None
    assert cost == pytest.approx(0.03)


@pytest.mark.parametrize(
    "status,raw_response,input_tokens,output_tokens",
    [
        ("provider_error", "", None, None),
        ("exhausted", "", None, None),
        ("ok", "", None, None),
    ],
)
async def test_persisted_llm_call_preserves_null_cost_for_unbilled_turns(
    session_factory: async_sessionmaker[AsyncSession],
    status: AdapterStatus,
    raw_response: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )

    _, cost = await _persist_result(
        session_factory,
        _result(
            model_id="openai/glm-4.7",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status,
            raw_response=raw_response,
        ),
        settings=settings,
    )

    assert cost is None


async def test_global_human_lane_spend_includes_fallback_priced_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )

    await _persist_result(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=500, output_tokens=500),
        settings=settings,
        human_seat=True,
    )

    async with session_factory() as session:
        spend = await global_human_lane_spend_usd(session)

    assert spend == pytest.approx(0.004)
