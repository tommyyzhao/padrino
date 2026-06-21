"""Cost governance for platform-absorbed human play (US-151).

Human play is platform-absorbed within a Moderate budget. This module bounds
spend WITHOUT ever killing an active game (the reviewers' "AI-only continuation
boots humans" anti-pattern is explicitly rejected): it stops NEW lobbies / NEW
LLM turns only.

Three layers compose here:

1. Per-user/day admission caps keyed on the human **principal** (an OAuth account
   principal, else the guest principal derived from the hashed guest token):
   a games/day cap, a joins/day cap, and an inference-$/day cap. Enforced at
   lobby **create / join / launch** admission via :func:`admit_human`.

2. A per-lobby cost cap + a global circuit breaker. :func:`lobby_breaker_open`
   returns True once a lobby's accrued inference cost meets its per-lobby cap OR
   cumulative human-lane inference meets the global breaker threshold. When the
   breaker is open the api/runner shell must STOP new lobbies / new LLM turns but
   MUST let active games run to completion.

3. A funding source recorded on every cost row (:class:`FundingSource`), defaulting
   to ``PLATFORM``; BYOK/sponsor are dormant in v1.

The curated ``human_eligible`` model pool and a fallback token-price table (used
when LiteLLM ``response_cost`` is None) also live here so admission and the
breaker price turns consistently.

This is the impure economics shell: it reads cost rows from the DB. It performs
no clock reads of its own — ``now`` is injected for deterministic tests, exactly
like :mod:`padrino.economics.admission`.
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import FundingSource
from padrino.db.models import (
    Game,
    GameSeat,
    HumanCostAdmission,
    HumanInferenceReservation,
    LlmCall,
    Lobby,
)
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.economics.human_cost_governance")


@dataclasses.dataclass(frozen=True)
class HumanAdmitDecision:
    """Typed result of a per-user human-play admission check.

    ``reason`` is a stable string callers branch on without string parsing:

        ``"admitted"``                 — all per-user caps passed
        ``"breaker_open"``             — the global cost breaker is open
        ``"daily_game_cap_reached"``   — games today >= games/day cap
        ``"daily_join_cap_reached"``   — joins today >= joins/day cap
        ``"daily_inference_cap_reached"`` — inference-$ today >= $/day cap

    On an ALLOWED decision the claimed slot ids are returned so the calling shell
    can bind them to the resulting lobby / member (US-190); an abandoned lobby or
    a member that leaves releases the bound slots so the caps count actual
    games/joins, not attempts. ``count_slot_id`` is the per-day game/join slot;
    ``inference_reservation_ids`` are the per-user + global $-budget slots.
    """

    allowed: bool
    reason: str
    count_slot_id: uuid.UUID | None = None
    inference_reservation_ids: tuple[uuid.UUID, ...] = ()


# Admission actions a per-user check can gate. ``launch`` and ``create`` both
# consume a games/day slot; ``join`` consumes a joins/day slot.
ACTION_CREATE = "create"
ACTION_JOIN = "join"
ACTION_LAUNCH = "launch"

_ADMISSION_BUCKET_GAME = "game"
_ADMISSION_BUCKET_JOIN = "join"
_HUMAN_LANE_SEAT_KINDS = ("HUMAN", "AI_TAKEOVER")


def price_turn_usd(
    settings: Settings,
    *,
    response_cost: float | None,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float:
    """Return the USD cost of one inference turn.

    Prefers LiteLLM's ``response_cost`` when present. When it is None (the
    provider returned no cost), falls back to the configured per-1K token-price
    table, looking up ``model`` and finally the ``default`` entry. Missing token
    counts coerce to zero (an unpriceable turn costs zero rather than crashing).
    """
    if response_cost is not None:
        return float(response_cost)

    table = settings.padrino_human_fallback_token_price_per_1k
    in_price, out_price = table.get(model, table["default"])
    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    return (in_tok / 1000.0) * in_price + (out_tok / 1000.0) * out_price


async def human_eligible_pool(session: AsyncSession, ruleset_id: str) -> list[str]:
    """Return the curated human-eligible agent-build-id pool for ``ruleset_id``.

    v1 curation: every ACTIVE agent build whose prompt targets the ruleset,
    ordered deterministically (created_at, id). This is the single source of
    truth the lobby auto-fill (US-149) and any human-lane seating consume so the
    "curated human_eligible model pool" is defined in one place.
    """
    from padrino.db.models import AgentBuild, PromptVersion

    stmt = (
        select(AgentBuild.id)
        .join(PromptVersion, AgentBuild.prompt_version_id == PromptVersion.id)
        .where(AgentBuild.active.is_(True))
        .where(PromptVersion.ruleset_id == ruleset_id)
        .order_by(AgentBuild.created_at, AgentBuild.id)
    )
    return [str(bid) for bid in (await session.execute(stmt)).scalars()]


async def _principal_games_today(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    today_start: datetime,
    tomorrow_start: datetime,
) -> int:
    """Count distinct human-lane games this principal launched/occupied today.

    A game is attributed to a principal via a ``GameSeat`` they occupy. Counting
    distinct games (not seats) keeps the cap a games/day cap.
    """
    stmt = (
        select(func.count(func.distinct(Game.id)))
        .join(GameSeat, GameSeat.game_id == Game.id)
        .where(GameSeat.occupant_principal_id == principal_id)
        .where(Game.created_at >= today_start)
        .where(Game.created_at < tomorrow_start)
    )
    return int((await session.execute(stmt)).scalar_one())


async def _principal_joins_today(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    today_start: datetime,
    tomorrow_start: datetime,
) -> int:
    """Count lobbies this principal joined today (via lobby_members.joined_at)."""
    from padrino.db.models import LobbyMember

    stmt = select(func.count(LobbyMember.id)).where(
        LobbyMember.principal_id == principal_id,
        LobbyMember.joined_at >= today_start,
        LobbyMember.joined_at < tomorrow_start,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _principal_inference_usd_today(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    today_start: datetime,
    tomorrow_start: datetime,
) -> float:
    """Sum inference $ attributed to this principal's games charged today.

    Cost is attributed to a principal through the games they occupy a seat in:
    an ``LlmCall`` belongs to the principal when its game has a ``GameSeat`` the
    principal occupies. The day boundary is the cost row's ``created_at`` so a
    game spanning UTC midnight charges spend to the day the LLM call occurred.
    Null costs coalesce to zero.
    """
    seat_games = (
        select(GameSeat.game_id).where(GameSeat.occupant_principal_id == principal_id).distinct()
    )
    stmt = (
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
        .where(LlmCall.game_id.in_(seat_games))
        .where(LlmCall.created_at >= today_start)
        .where(LlmCall.created_at < tomorrow_start)
    )
    value = (await session.execute(stmt)).scalar_one()
    return float(value) if value is not None else 0.0


async def global_human_lane_spend_usd(session: AsyncSession) -> float:
    """Return cumulative inference $ across all human-lane games.

    A human-lane game is one with at least one HUMAN or AI_TAKEOVER
    ``GameSeat``. Null costs coalesce to zero.
    """
    human_games = (
        select(GameSeat.game_id).where(GameSeat.seat_kind.in_(_HUMAN_LANE_SEAT_KINDS)).distinct()
    ).subquery()
    stmt = select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0)).where(
        LlmCall.game_id.in_(select(human_games.c.game_id))
    )
    value = (await session.execute(stmt)).scalar_one()
    return float(value) if value is not None else 0.0


async def lobby_accrued_usd(session: AsyncSession, lobby: Lobby) -> float:
    """Return inference $ accrued by the game a lobby launched (0 before launch)."""
    if lobby.game_id is None:
        return 0.0
    stmt = select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0)).where(
        LlmCall.game_id == lobby.game_id
    )
    value = (await session.execute(stmt)).scalar_one()
    return float(value) if value is not None else 0.0


async def global_breaker_open(session: AsyncSession, settings: Settings) -> bool:
    """Return True once cumulative human-lane spend meets the global breaker.

    When open the api/runner shell STOPS new lobbies / new LLM turns; it MUST
    NOT kill an active game.
    """
    spent = await global_human_lane_spend_usd(session)
    if spent >= settings.padrino_human_global_lobby_cost_breaker_usd:
        _logger.warning(
            "human_cost.breaker.open",
            scope="global",
            spent_usd=round(spent, 6),
            cap_usd=settings.padrino_human_global_lobby_cost_breaker_usd,
        )
        return True
    return False


async def lobby_breaker_open(session: AsyncSession, settings: Settings, lobby: Lobby) -> bool:
    """Return True when this lobby's cost cap OR the global breaker is breached.

    On True the runner stops issuing NEW LLM turns for the lobby's game, but the
    active game still finishes its in-flight resolution (turn-level throttle,
    never a game kill).
    """
    if await global_breaker_open(session, settings):
        return True
    accrued = await lobby_accrued_usd(session, lobby)
    if accrued >= settings.padrino_human_lobby_cost_cap_usd:
        _logger.warning(
            "human_cost.breaker.open",
            scope="lobby",
            lobby_id=str(lobby.id),
            accrued_usd=round(accrued, 6),
            cap_usd=settings.padrino_human_lobby_cost_cap_usd,
        )
        return True
    return False


def _admission_bucket(action: str) -> str:
    if action == ACTION_JOIN:
        return _ADMISSION_BUCKET_JOIN
    return _ADMISSION_BUCKET_GAME


def _next_free_index(*, implicit_used: int, physical: set[int]) -> int:
    """Lowest non-negative slot index claimed by neither implicit nor physical use.

    Implicit (legacy) use occupies ``0..implicit_used-1``; physical rows occupy
    their stored indices (RELEASED rows included, so a freed index is never
    re-inserted — that would collide on the unique constraint). The cap is
    enforced separately on LIVE (unreleased) count, so the chosen index may grow
    above the cap over a day; that is fine — the constraint only needs distinct
    indices per scope/day.
    """
    taken = set(range(max(implicit_used, 0))) | physical
    index = 0
    while index in taken:
        index += 1
    return index


async def _admission_slot_indices(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    admission_day: date,
    bucket: str,
) -> tuple[set[int], set[int]]:
    """Return ``(live_indices, all_indices)`` for a principal/day/bucket.

    ``live_indices`` exclude RELEASED rows (those free their cap slot for re-use);
    ``all_indices`` include them (a released row still physically occupies its
    unique ``slot_index`` so a new claim must pick a different index).
    """
    stmt = select(HumanCostAdmission.slot_index, HumanCostAdmission.released_at).where(
        HumanCostAdmission.principal_id == principal_id,
        HumanCostAdmission.admission_day == admission_day,
        HumanCostAdmission.bucket == bucket,
    )
    live: set[int] = set()
    everything: set[int] = set()
    for slot_index, released_at in (await session.execute(stmt)).all():
        everything.add(int(slot_index))
        if released_at is None:
            live.add(int(slot_index))
    return live, everything


async def _claim_admission_slot(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    action: str,
    admission_day: date,
    admitted_at: datetime,
    legacy_count: int,
    cap: int,
) -> uuid.UUID | None:
    """Atomically claim one finite per-principal/day admission slot.

    Existing pre-US-165 game/member rows are treated as implicit slots so legacy
    data still counts. New admissions are explicit rows protected by
    ``uq_human_cost_admission_slot``; concurrent callers that race for the same
    slot retry until no slots remain. A RELEASED slot frees its cap budget but
    keeps its physical index, so a re-claim picks a fresh index. Returns the
    claimed row id (so the caller can bind it to the resulting lobby/member), or
    ``None`` when the cap is exhausted.
    """
    if cap <= 0:
        return None

    bucket = _admission_bucket(action)
    implicit_used = min(max(legacy_count, 0), cap)
    for _ in range(cap + 1):
        live, everything = await _admission_slot_indices(
            session, principal_id, admission_day=admission_day, bucket=bucket
        )
        if implicit_used + len(live) >= cap:
            return None
        slot_index = _next_free_index(implicit_used=implicit_used, physical=everything)

        row = HumanCostAdmission(
            principal_id=principal_id,
            admission_day=admission_day,
            action=action,
            bucket=bucket,
            slot_index=slot_index,
            admitted_at=admitted_at,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue
        return row.id

    return None


def _budget_slot_count(budget_usd: float, reserve_usd: float) -> int:
    """Number of discrete reservation slots a $ budget admits (>= 0)."""
    if budget_usd <= 0.0 or reserve_usd <= 0.0:
        return 0
    return math.floor(budget_usd / reserve_usd)


def _implicit_budget_used(spent_usd: float, reserve_usd: float) -> int:
    """Reservation slots already consumed by ALREADY-CHARGED spend.

    Charged spend is rounded UP to a whole slot so the atomic reservation never
    under-counts real money already burned (fail-closed toward the budget).
    """
    if spent_usd <= 0.0 or reserve_usd <= 0.0:
        return 0
    return math.ceil(spent_usd / reserve_usd)


async def _inference_slot_indices(
    session: AsyncSession,
    *,
    scope_key: str,
    reservation_day: date,
) -> tuple[set[int], set[int]]:
    """Return ``(live_indices, all_indices)`` for a $-reservation scope/day."""
    stmt = select(
        HumanInferenceReservation.slot_index, HumanInferenceReservation.released_at
    ).where(
        HumanInferenceReservation.scope_key == scope_key,
        HumanInferenceReservation.reservation_day == reservation_day,
    )
    live: set[int] = set()
    everything: set[int] = set()
    for slot_index, released_at in (await session.execute(stmt)).all():
        everything.add(int(slot_index))
        if released_at is None:
            live.add(int(slot_index))
    return live, everything


async def _claim_inference_slot(
    session: AsyncSession,
    *,
    scope_key: str,
    reservation_day: date,
    reserved_at: datetime,
    spent_usd: float,
    budget_usd: float,
    reserve_usd: float,
) -> uuid.UUID | None:
    """Atomically reserve one slice of a $ budget; ``None`` when none remain.

    The budget is divided into ``floor(budget / reserve)`` finite slots. Already
    charged spend implicitly consumes the lowest slots; an explicit reservation
    row (unique on ``scope_key, reservation_day, slot_index``) claims the next
    free index. Concurrent claimers race the unique constraint and retry, so the
    number of live reservations + implicit-used can never exceed the slot count —
    i.e. concurrent admissions cannot overshoot the $ ceiling. A RELEASED row
    frees its budget but keeps its physical index (a re-claim picks a fresh one).
    """
    cap = _budget_slot_count(budget_usd, reserve_usd)
    if cap <= 0:
        return None
    implicit_used = min(_implicit_budget_used(spent_usd, reserve_usd), cap)
    for _ in range(cap + 1):
        live, everything = await _inference_slot_indices(
            session, scope_key=scope_key, reservation_day=reservation_day
        )
        if implicit_used + len(live) >= cap:
            return None
        slot_index = _next_free_index(implicit_used=implicit_used, physical=everything)
        row = HumanInferenceReservation(
            scope_key=scope_key,
            reservation_day=reservation_day,
            slot_index=slot_index,
            reserved_at=reserved_at,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue
        return row.id
    return None


async def bind_admission_slots(
    session: AsyncSession,
    decision: HumanAdmitDecision,
    *,
    lobby_id: uuid.UUID | None = None,
    lobby_member_id: uuid.UUID | None = None,
) -> None:
    """Tie an admitted decision's slots to the lobby/member it produced.

    Binding lets a later abandonment (idle auto-cancel) or member departure
    RELEASE exactly the slots an action consumed, so the per-day caps count
    actual games/joins, not attempts (US-190).
    """
    if decision.count_slot_id is not None:
        await session.execute(
            update(HumanCostAdmission)
            .where(HumanCostAdmission.id == decision.count_slot_id)
            .values(lobby_id=lobby_id, lobby_member_id=lobby_member_id)
        )
    for reservation_id in decision.inference_reservation_ids:
        await session.execute(
            update(HumanInferenceReservation)
            .where(HumanInferenceReservation.id == reservation_id)
            .values(lobby_id=lobby_id, lobby_member_id=lobby_member_id)
        )


async def release_admission_for_lobby(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    released_at: datetime,
) -> None:
    """Release every UNRELEASED admission slot bound to an abandoned lobby."""
    await session.execute(
        update(HumanCostAdmission)
        .where(
            HumanCostAdmission.lobby_id == lobby_id,
            HumanCostAdmission.released_at.is_(None),
        )
        .values(released_at=released_at)
    )
    await session.execute(
        update(HumanInferenceReservation)
        .where(
            HumanInferenceReservation.lobby_id == lobby_id,
            HumanInferenceReservation.released_at.is_(None),
        )
        .values(released_at=released_at)
    )


async def release_admission_for_member(
    session: AsyncSession,
    *,
    lobby_member_id: uuid.UUID,
    released_at: datetime,
) -> None:
    """Release every UNRELEASED admission slot bound to a departed member."""
    await session.execute(
        update(HumanCostAdmission)
        .where(
            HumanCostAdmission.lobby_member_id == lobby_member_id,
            HumanCostAdmission.released_at.is_(None),
        )
        .values(released_at=released_at)
    )
    await session.execute(
        update(HumanInferenceReservation)
        .where(
            HumanInferenceReservation.lobby_member_id == lobby_member_id,
            HumanInferenceReservation.released_at.is_(None),
        )
        .values(released_at=released_at)
    )


async def admit_human(
    session: AsyncSession,
    settings: Settings,
    *,
    principal_id: uuid.UUID,
    action: str,
    now: datetime | None = None,
) -> HumanAdmitDecision:
    """Gate a human ``create`` / ``join`` / ``launch`` action against per-user caps.

    Evaluation order: global breaker → daily inference-$ cap → the action's daily
    count cap (games for create/launch, joins for join). The first failing check
    short-circuits with a structured ``human_cost.admission.denied`` log.

    ``now`` is injectable for deterministic tests (defaults to UTC wall clock,
    read only in this impure shell).
    """
    if now is None:
        now = datetime.now(tz=UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    reservation_day = today_start.date()
    reserve_usd = settings.padrino_human_admission_inference_reserve_usd

    # Track $-reservations claimed so far so a later short-circuit can roll them
    # back (delete the uncommitted rows) instead of leaking a held budget slice.
    claimed_reservations: list[uuid.UUID] = []

    async def _rollback_reservations() -> None:
        for reservation_id in claimed_reservations:
            await session.execute(
                delete(HumanInferenceReservation).where(
                    HumanInferenceReservation.id == reservation_id
                )
            )

    # 1. Global cost breaker — atomic reservation against the global $ budget so
    #    concurrent admissions cannot overshoot it (US-190). Already-charged
    #    human-lane spend implicitly consumes the lowest slots.
    global_spent = await global_human_lane_spend_usd(session)
    global_slot = await _claim_inference_slot(
        session,
        scope_key="GLOBAL",
        reservation_day=reservation_day,
        reserved_at=now,
        spent_usd=global_spent,
        budget_usd=settings.padrino_human_global_lobby_cost_breaker_usd,
        reserve_usd=reserve_usd,
    )
    if global_slot is None:
        _logger.warning("human_cost.admission.denied", reason="breaker_open", action=action)
        return HumanAdmitDecision(allowed=False, reason="breaker_open")
    claimed_reservations.append(global_slot)

    # 2. Per-user/day inference-$ cap — atomic reservation against this
    #    principal's remaining daily $ budget.
    spent = await _principal_inference_usd_today(
        session, principal_id, today_start=today_start, tomorrow_start=tomorrow_start
    )
    user_slot = await _claim_inference_slot(
        session,
        scope_key=principal_id.hex,
        reservation_day=reservation_day,
        reserved_at=now,
        spent_usd=spent,
        budget_usd=settings.padrino_human_max_inference_usd_per_user_per_day,
        reserve_usd=reserve_usd,
    )
    if user_slot is None:
        await _rollback_reservations()
        _logger.warning(
            "human_cost.admission.denied",
            reason="daily_inference_cap_reached",
            action=action,
            spent_usd=round(spent, 6),
        )
        return HumanAdmitDecision(allowed=False, reason="daily_inference_cap_reached")
    claimed_reservations.append(user_slot)

    # 3. The action's daily count cap (games for create/launch, joins for join).
    if action == ACTION_JOIN:
        joins = await _principal_joins_today(
            session, principal_id, today_start=today_start, tomorrow_start=tomorrow_start
        )
        slot = await _claim_admission_slot(
            session,
            principal_id,
            action=action,
            admission_day=reservation_day,
            admitted_at=now,
            legacy_count=joins,
            cap=settings.padrino_human_max_joins_per_user_per_day,
        )
        if slot is None:
            await _rollback_reservations()
            _logger.warning(
                "human_cost.admission.denied",
                reason="daily_join_cap_reached",
                action=action,
                joins=joins,
            )
            return HumanAdmitDecision(allowed=False, reason="daily_join_cap_reached")
    else:
        games = await _principal_games_today(
            session, principal_id, today_start=today_start, tomorrow_start=tomorrow_start
        )
        slot = await _claim_admission_slot(
            session,
            principal_id,
            action=action,
            admission_day=reservation_day,
            admitted_at=now,
            legacy_count=games,
            cap=settings.padrino_human_max_games_per_user_per_day,
        )
        if slot is None:
            await _rollback_reservations()
            _logger.warning(
                "human_cost.admission.denied",
                reason="daily_game_cap_reached",
                action=action,
                games=games,
            )
            return HumanAdmitDecision(allowed=False, reason="daily_game_cap_reached")

    _logger.info("human_cost.admission.allowed", action=action)
    return HumanAdmitDecision(
        allowed=True,
        reason="admitted",
        count_slot_id=slot,
        inference_reservation_ids=tuple(claimed_reservations),
    )


__all__ = [
    "ACTION_CREATE",
    "ACTION_JOIN",
    "ACTION_LAUNCH",
    "FundingSource",
    "HumanAdmitDecision",
    "admit_human",
    "bind_admission_slots",
    "global_breaker_open",
    "global_human_lane_spend_usd",
    "human_eligible_pool",
    "lobby_accrued_usd",
    "lobby_breaker_open",
    "price_turn_usd",
    "release_admission_for_lobby",
    "release_admission_for_member",
]
