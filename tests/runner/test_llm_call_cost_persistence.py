"""US-232: token-price fallback for persisted LLM call costs."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import GameSeat, LlmCall
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.economics.human_cost_governance import (
    PRICE_BASIS_FALLBACK_TABLE,
    PRICE_BASIS_PROVIDER_RESPONSE_COST,
    fallback_price_table_version,
    global_human_lane_spend_usd,
)
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


async def _persist_call(
    session_factory: async_sessionmaker[AsyncSession],
    result: AdapterResult,
    *,
    settings: Settings,
    human_seat: bool = False,
) -> tuple[uuid.UUID, LlmCall]:
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
    return game_id, rows[0]


async def _persist_result(
    session_factory: async_sessionmaker[AsyncSession],
    result: AdapterResult,
    *,
    settings: Settings,
    human_seat: bool = False,
) -> tuple[uuid.UUID, float | None]:
    game_id, row = await _persist_call(
        session_factory,
        result,
        settings=settings,
        human_seat=human_seat,
    )
    persisted_cost = row.cost_usd
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


async def test_provider_priced_llm_call_stamps_provider_basis(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (10.0, 10.0),
        },
    )

    _, row = await _persist_call(
        session_factory,
        _result(
            model_id="openai/glm-4.7",
            input_tokens=10_000,
            output_tokens=10_000,
            cost_usd=0.42,
        ),
        settings=settings,
    )

    assert row.cost_usd == pytest.approx(0.42)
    assert row.price_basis == PRICE_BASIS_PROVIDER_RESPONSE_COST
    assert row.price_table_version is None


async def test_fallback_priced_llm_call_stamps_table_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )

    _, row = await _persist_call(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1234, output_tokens=250),
        settings=settings,
    )

    assert row.cost_usd == pytest.approx(0.003968)
    assert row.price_basis == PRICE_BASIS_FALLBACK_TABLE
    assert row.price_table_version == fallback_price_table_version(settings)


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


async def test_persisted_fallback_price_stamps_are_immutable_after_rate_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )

    _, first = await _persist_call(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1000, output_tokens=1000),
        settings=settings,
    )
    assert first.cost_usd is not None
    assert first.price_table_version is not None
    first_cost = float(first.cost_usd)
    first_version = first.price_table_version

    settings.padrino_human_fallback_token_price_per_1k = {
        "default": (0.0, 0.0),
        "openai/glm-4.7": (0.01, 0.02),
    }
    _, second = await _persist_call(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1000, output_tokens=1000),
        settings=settings,
    )

    async with session_factory() as session:
        unchanged = await session.get(LlmCall, first.id)
    assert unchanged is not None
    assert unchanged.cost_usd == pytest.approx(first_cost)
    assert unchanged.price_table_version == first_version

    assert second.cost_usd == pytest.approx(0.03)
    assert second.price_table_version != first_version


async def test_fallback_spend_is_groupable_by_stamped_price_table_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.002, 0.006),
        },
    )
    second_settings = Settings(
        padrino_human_fallback_token_price_per_1k={
            "default": (0.0, 0.0),
            "openai/glm-4.7": (0.01, 0.02),
        },
    )

    await _persist_call(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1000, output_tokens=1000),
        settings=first_settings,
    )
    await _persist_call(
        session_factory,
        _result(model_id="openai/glm-4.7", input_tokens=1000, output_tokens=1000),
        settings=second_settings,
    )

    async with session_factory() as session:
        result = await session.execute(
            select(LlmCall.price_table_version, func.sum(LlmCall.cost_usd))
            .where(LlmCall.price_basis == PRICE_BASIS_FALLBACK_TABLE)
            .group_by(LlmCall.price_table_version)
        )
    spend_by_version = {version: float(spend) for version, spend in result.all()}

    assert spend_by_version == {
        fallback_price_table_version(first_settings): pytest.approx(0.008),
        fallback_price_table_version(second_settings): pytest.approx(0.03),
    }


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
