"""US-361 runtime behavior when a frozen platform game becomes unfunded."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import (
    Game,
    OperatorCompletionReserveSettlement,
    Principal,
    PromotionalCreditReservation,
)
from padrino.economics.platform_completion_adapter import PlatformCompletionReserveAdapter
from padrino.llm.adapter import AdapterResult, ProviderNoContestError

_NOW = datetime(2026, 7, 12, 23, 0, tzinfo=UTC)


def _observation() -> Observation:
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id="G-COMPLETION",
        phase="DAY_1_VOTE",
        day=1,
        round=0,
        you=YouInfo(player_id="P01", alive=True, role=Role.VILLAGER, faction=Faction.TOWN),
        alive_players=("P01", "P02"),
        dead_players=(),
        public_events=(),
        private_events=(),
        legal_actions=LegalActions(allowed_action_types=[ActionType.VOTE], legal_targets=["P02"]),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=500,
            private_message_max_chars=500,
            memory_update_max_chars=500,
        ),
    )


def _provider_result() -> AdapterResult:
    response = AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=ActionType.VOTE, target="P02"),
        memory_update="",
        rationale_summary=None,
    )
    return AdapterResult(
        raw_response=response.model_dump_json(),
        parsed_response=response,
        latency_ms=1,
        cost_usd=0.000011,
        model_id="openai/frozen-model",
    )


class _OneResultAdapter:
    async def complete(self, observation: Observation) -> AdapterResult:
        return _provider_result()


async def _reservation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    policy: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed=f"platform-completion-{uuid.uuid4()}",
            status="RUNNING",
        )
        session.add_all([principal, game])
        await session.flush()
        reservation = PromotionalCreditReservation(
            principal_id=principal.id,
            lobby_id=None,
            game_id=game.id,
            estimate_hash="a" * 64,
            catalog_hash="b" * 64,
            rules_hash="c" * 64,
            reserved_micros=7,
            consumed_micros=0,
            released_micros=0,
            status="ACTIVE",
            idempotency_key=f"platform-completion:{game.id}",
            reserved_at=_NOW,
            updated_at=_NOW,
            terminal_at=None,
            completion_reserve_per_game_cap_micros=3,
            completion_reserve_unfunded_policy=policy,
        )
        session.add(reservation)
        await session.flush()
        return game.id, reservation.id


async def test_unfunded_platform_call_is_coerced_without_route_or_model_swap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, reservation_id = await _reservation(session_factory, policy="COERCE")
    adapter = PlatformCompletionReserveAdapter(
        _OneResultAdapter(),
        session_factory=session_factory,
        game_id=game_id,
        reservation_id=reservation_id,
        global_cap_micros=3,
        now=lambda: _NOW,
    )

    result = await adapter.complete(_observation())

    assert result.status == "exhausted"
    assert result.error == "provider_unfunded"
    assert result.model_id == "openai/frozen-model"
    async with session_factory() as session:
        settlement = await session.scalar(
            select(OperatorCompletionReserveSettlement).where(
                OperatorCompletionReserveSettlement.game_id == game_id
            )
        )
    assert settlement is not None
    assert settlement.promotional_charge_micros == 7
    assert settlement.completion_reserve_charge_micros == 3
    assert settlement.unfunded_micros == 1
    assert settlement.unfunded_policy == "COERCE"


async def test_no_contest_policy_uses_existing_terminal_provider_failure_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, reservation_id = await _reservation(session_factory, policy="NO_CONTEST")
    adapter = PlatformCompletionReserveAdapter(
        _OneResultAdapter(),
        session_factory=session_factory,
        game_id=game_id,
        reservation_id=reservation_id,
        global_cap_micros=0,
        now=lambda: _NOW,
    )

    with pytest.raises(ProviderNoContestError, match="provider_unfunded") as raised:
        await adapter.complete(_observation())

    assert raised.value.result is not None
    assert raised.value.result.model_id == "openai/frozen-model"
    assert raised.value.result.error == "provider_unfunded"
