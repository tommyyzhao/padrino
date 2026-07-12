"""US-362 durable call charging and terminal reservation settlement coverage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.enums import ActionType, Faction, FundingSource, Role
from padrino.core.observations import MessageLimits, Observation, YouInfo
from padrino.db.models import (
    Game,
    LlmCall,
    OperatorCompletionReserveSettlement,
    Principal,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
)
from padrino.db.repositories import llm_calls as llm_calls_repo
from padrino.db.repositories import promotional_credit_ledger
from padrino.economics.call_settlement import (
    funding_cost_report,
    reconcile_game_call_settlements,
    settle_persisted_llm_call,
    settle_terminal_reservation,
)
from padrino.llm.adapter import AdapterResult
from padrino.runner.game_runner import GamePersistence, _persist_llm_call
from padrino.settings import Settings

_NOW = datetime(2026, 7, 12, 23, 30, tzinfo=UTC)


async def _funded_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        principal = Principal(kind="account")
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed=f"call-settlement-{uuid.uuid4()}",
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
            reserved_micros=10,
            consumed_micros=0,
            released_micros=0,
            status="ACTIVE",
            idempotency_key=f"reservation:{game.id}",
            reserved_at=_NOW,
            updated_at=_NOW,
            terminal_at=None,
            completion_reserve_per_game_cap_micros=3,
            completion_reserve_unfunded_policy="COERCE",
        )
        session.add(reservation)
        await session.flush()
        await promotional_credit_ledger.append_credit_entry(
            session,
            principal_id=principal.id,
            reservation_id=reservation.id,
            game_id=game.id,
            entry_type=promotional_credit_ledger.CreditLedgerEntryType.RESERVATION,
            amount_micros=-10,
            idempotency_key=f"hold:{game.id}",
            reason="custom_game_admission",
            now=_NOW,
        )
        return game.id, principal.id, reservation.id


async def _call(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    funding_source: FundingSource = FundingSource.PLATFORM,
) -> LlmCall:
    return await llm_calls_repo.record_call(
        session,
        game_id=game_id,
        public_player_id="P01",
        phase="DAY_1_VOTE",
        request_json={},
        request_prompt_hash="a" * 64,
        status="ok",
        cost_usd=0.000011,
        price_basis="provider_response_cost",
        funding_source=funding_source.value,
        openrouter_requested_model_slug="openai/frozen-model",
    )


def _observation() -> Observation:
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id="G-CALL-SETTLEMENT",
        phase="DAY_1_VOTE",
        day=1,
        round=0,
        you=YouInfo(
            player_id="P01",
            alive=True,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
        ),
        alive_players=("P01", "P02"),
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


def _result() -> AdapterResult:
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
        latency_ms=1,
        cost_usd=0.000011,
        model_id="openai/frozen-model",
    )


async def test_recording_layer_commits_call_and_charge_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _principal_id, _reservation_id = await _funded_game(session_factory)
    persisted, no_contest = await _persist_llm_call(
        GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            settings=Settings(padrino_openrouter_completion_reserve_global_micros=3),
            settlement_clock=lambda: _NOW,
        ),
        _observation(),
        _result(),
    )

    assert persisted.status == "ok"
    assert no_contest is False
    async with session_factory() as session:
        calls = await llm_calls_repo.list_for_game(session, game_id)
        settlement = await session.scalar(
            select(OperatorCompletionReserveSettlement).where(
                OperatorCompletionReserveSettlement.game_id == game_id
            )
        )
    assert len(calls) == 1
    assert calls[0].charge_amount_micros == 11
    assert settlement is not None
    assert settlement.llm_call_id == calls[0].id


async def test_platform_call_charge_converts_the_hold_and_terminal_release_is_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _principal_id, reservation_id = await _funded_game(session_factory)

    async with session_factory() as session, session.begin():
        call = await _call(session, game_id=game_id)
        charged = await settle_persisted_llm_call(
            session,
            call=call,
            global_cap_micros=3,
            now=_NOW,
        )
        assert charged is not None
        assert charged.call_cost_micros == 11
        assert charged.promotional_charge_micros == 10
        assert charged.operator_completion_charge_micros == 1
        assert call.charge_idempotency_key == f"llm-call-charge:{call.id}"
        assert call.charge_amount_micros == 11

        duplicate = await settle_persisted_llm_call(
            session,
            call=call,
            global_cap_micros=3,
            now=_NOW,
        )
        assert duplicate is not None
        assert duplicate == charged

        terminal = await settle_terminal_reservation(
            session,
            game_id=game_id,
            now=_NOW,
        )
        assert terminal is not None
        assert terminal.released_micros == 0
        assert terminal.status == "SETTLED"
        assert await settle_terminal_reservation(session, game_id=game_id, now=_NOW) == terminal

    async with session_factory() as session:
        reservation = await session.get(PromotionalCreditReservation, reservation_id)
        assert reservation is not None
        assert reservation.consumed_micros == 10
        assert reservation.released_micros == 0
        entries = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry)
                    .where(PromotionalCreditLedgerEntry.reservation_id == reservation_id)
                    .order_by(
                        PromotionalCreditLedgerEntry.created_at, PromotionalCreditLedgerEntry.id
                    )
                )
            ).scalars()
        )
        settlement = await session.scalar(
            select(OperatorCompletionReserveSettlement).where(
                OperatorCompletionReserveSettlement.game_id == game_id
            )
        )

    assert settlement is not None
    assert settlement.llm_call_id == call.id
    assert sorted((entry.entry_type, entry.amount_micros) for entry in entries) == [
        ("CHARGE", -10),
        ("RELEASE", 10),
        ("RESERVATION", -10),
    ]
    async with session_factory() as session:
        report = await funding_cost_report(session, game_id=game_id)
    assert report.total_micros == 11
    assert report.lines[0].funding_source == FundingSource.PLATFORM.value


async def test_byok_call_is_audited_without_promotional_debit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _principal_id, reservation_id = await _funded_game(session_factory)

    async with session_factory() as session, session.begin():
        call = await _call(session, game_id=game_id, funding_source=FundingSource.BYOK_OWNER)
        charged = await settle_persisted_llm_call(
            session,
            call=call,
            global_cap_micros=3,
            now=_NOW,
        )
        assert charged is not None
        assert charged.funding_source is FundingSource.BYOK_OWNER
        assert charged.promotional_charge_micros == 0
        assert charged.operator_completion_charge_micros == 0
        assert call.charge_amount_micros == 11

    async with session_factory() as session:
        reservation = await session.get(PromotionalCreditReservation, reservation_id)
        entries = list(
            (
                await session.execute(
                    select(PromotionalCreditLedgerEntry).where(
                        PromotionalCreditLedgerEntry.reservation_id == reservation_id
                    )
                )
            ).scalars()
        )
    assert reservation is not None
    assert reservation.consumed_micros == 0
    assert [(entry.entry_type, entry.amount_micros) for entry in entries] == [("RESERVATION", -10)]


async def test_reconciliation_charges_a_committed_call_once_before_terminal_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_id, _principal_id, reservation_id = await _funded_game(session_factory)
    async with session_factory() as session, session.begin():
        await _call(session, game_id=game_id)

    async with session_factory() as session, session.begin():
        reconciled = await reconcile_game_call_settlements(
            session,
            game_id=game_id,
            global_cap_micros=3,
            now=_NOW,
        )
        assert len(reconciled) == 1
        terminal = await settle_terminal_reservation(session, game_id=game_id, now=_NOW)
        assert terminal is not None

    async with session_factory() as session:
        reservation = await session.get(PromotionalCreditReservation, reservation_id)
        calls = await llm_calls_repo.list_for_game(session, game_id)
    assert reservation is not None
    assert reservation.status == "SETTLED"
    assert len(calls) == 1
    assert calls[0].charge_idempotency_key == f"llm-call-charge:{calls[0].id}"
