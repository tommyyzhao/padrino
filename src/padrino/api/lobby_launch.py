"""Launch handoff: materialize a real human-lane game from a lobby (US-149).

When a host launches a locked lobby, this module:

1. Resolves the lobby's seat layout (HUMAN seats reserved by members, AI seats
   the host pre-picked a model for, and EMPTY AI seats).
2. Fills the empty AI seats deterministically from the curated human-eligible
   build pool via the PURE :func:`padrino.core.lobby.autofill.autofill_empty_seats`
   (SeededRng over the lobby seed — no clock/random here).
3. Materializes a :class:`~padrino.db.models.Game` (gauntlet-less, so the
   benchmark scheduler never claims it) plus one :class:`~padrino.db.models.GameSeat`
   per seat: HUMAN seats carry ``occupant_principal_id`` + ``seat_kind='HUMAN'``;
   AI seats carry ``agent_build_id`` + ``seat_kind='AI'``. The presence of a HUMAN
   seat is exactly what makes the human worker lane (US-132) pick the game up.
4. Flips the lobby to ``LAUNCHED`` and records ``game_id``.

The handoff is single-fire / idempotent: a lobby that already launched returns
its existing ``game_id`` and writes nothing. Roles/factions are assigned
deterministically from the game seed via the same pure
:func:`padrino.core.engine.role_assignment.assign_roles` the runner uses, so the
seat rows agree with the ``RolesAssigned`` event the game later emits (the runner
skips its own seat backfill because human-lane games carry no per-seat agent
builds).

This is the impure shell: it reads/writes the DB. The seat selection itself is
pure (autofill + role assignment).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.lobby_readiness import (
    available_auto_fill_build_count,
    evaluate_lobby_rows,
)
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import Seat
from padrino.core.enums import LobbySeatKind, LobbyStatus, SeatKind
from padrino.core.lobby.autofill import (
    NotEnoughCuratedModelsError,
    autofill_empty_seats,
)
from padrino.core.lobby.strangers_only import (
    RankedSeatingViolation,
    assert_strangers_only_ranked,
)
from padrino.core.observation_privacy import is_anonymous
from padrino.core.seating import seat_permutation
from padrino.db.models import GameSeat, Lobby, LobbyMember, LobbySeat
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import lobbies as lobbies_repo
from padrino.db.rules_snapshots import runtime_ruleset_from_pair
from padrino.openrouter.byok_runtime import (
    ByokRuntimeError,
    validate_byok_lobby_credentials_for_lobby,
)
from padrino.openrouter.funding_preflight import (
    admit_lobby_funding_before_game,
    bind_funding_reservation_to_game,
)
from padrino.openrouter.lobby_model_selection import (
    LobbyModelSelectionError,
    materialize_frozen_lobby_model_selection,
)
from padrino.settings import Settings


class LobbyNotLaunchableError(Exception):
    """Raised when a lobby's status does not permit a launch (not LOCKED)."""


class AutoFillPoolError(Exception):
    """Raised when the curated pool cannot fill the lobby's empty AI seats."""


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """Outcome of a launch handoff."""

    game_id: uuid.UUID
    #: True when this call materialized the game; False on an idempotent replay
    #: of an already-launched lobby.
    created: bool


def _derive_game_seed(lobby_seed: str) -> str:
    """Derive a deterministic game seed from the lobby seed.

    A distinct namespace from the auto-fill seed so the role shuffle and the
    model placement draw from independent streams.
    """
    return hashlib.sha256(b"game:" + lobby_seed.encode("utf-8")).hexdigest()


async def _curated_roster(session: AsyncSession, ruleset_id: str) -> list[str]:
    """Return the curated human-eligible build-id pool for ``ruleset_id``.

    Delegates to the single source of truth in
    :func:`padrino.economics.human_cost_governance.human_eligible_pool` (US-151):
    every ACTIVE agent build whose prompt targets this ruleset, ordered
    deterministically (created_at, id).
    """
    from padrino.economics.human_cost_governance import human_eligible_pool

    return await human_eligible_pool(session, ruleset_id)


async def launch_lobby(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    source_ip_hash: str | None = None,
    now: datetime | None = None,
    stale_seconds: float | None = None,
    funding_quote_hash: str | None = None,
    byok_max_estimate_micros: int | None = None,
    funding_settings: Settings | None = None,
) -> LaunchResult:
    """Materialize a human-lane game from a LOCKED lobby (idempotent).

    Raises:
        LobbyNotLaunchableError: the lobby is not LOCKED (and not already LAUNCHED).
        AutoFillPoolError: the curated pool cannot fill the empty AI seats.
    """
    lobby = await lobbies_repo.get_lobby(session, lobby_id)
    if lobby is None:
        raise LobbyNotLaunchableError("lobby_not_found")

    # Idempotent replay: an already-launched lobby returns its game, untouched.
    if lobby.status == LobbyStatus.LAUNCHED.value and lobby.game_id is not None:
        return LaunchResult(game_id=lobby.game_id, created=False)

    if lobby.status != LobbyStatus.LOCKED.value:
        raise LobbyNotLaunchableError("lobby_not_locked")

    ruleset = runtime_ruleset_from_pair(
        ruleset_id=lobby.ruleset_id,
        rules_snapshot_json=lobby.rules_snapshot_json,
        rules_snapshot_hash=lobby.rules_snapshot_hash,
    )
    members = await lobbies_repo.list_members(session, lobby_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    structural_readiness = evaluate_lobby_rows(
        members=members,
        seats=seats,
        expected_player_count=ruleset.PLAYER_COUNT,
        available_ai_build_count=None,
        now=now,
        stale_seconds=stale_seconds,
    )
    if not structural_readiness.allowed:
        raise LobbyNotLaunchableError(structural_readiness.reasons[0].value)

    # US-348 selections are resolved against an immutable catalog while the
    # lobby is mutable, then materialized only here.  A later catalog refresh
    # therefore cannot replace a locked seat's chosen slug, prompt/schema,
    # inference parameters, routing preferences, or funding-owner reference.
    materialization_now = now if now is not None else datetime.now(UTC)
    for seat in seats:
        if seat.seat_kind != LobbySeatKind.AI.value or seat.model_selection is None:
            continue
        try:
            seat.agent_build_id = await materialize_frozen_lobby_model_selection(
                session,
                selection_json_value=seat.model_selection,
                now=materialization_now,
            )
        except (LobbyModelSelectionError, ValueError) as exc:
            raise LobbyNotLaunchableError(str(exc) or "frozen_model_unavailable") from exc
    # A frozen BYOK selection is usable only while its host-owned envelope is
    # active. This is a lifecycle check, not a decryption: the raw key is read
    # exclusively later while constructing the per-seat AI adapters.
    try:
        await validate_byok_lobby_credentials_for_lobby(session, lobby_id=lobby.id)
    except ByokRuntimeError as exc:
        raise LobbyNotLaunchableError(exc.reason) from exc

    league = await leagues_repo.get(session, lobby.league_id)
    human_seat_count = sum(1 for seat in seats if seat.seat_kind == LobbySeatKind.HUMAN.value)
    try:
        assert_strangers_only_ranked(
            lobby_is_ranked=league.ranked if league is not None else False,
            human_seat_count=human_seat_count,
        )
    except RankedSeatingViolation as exc:
        raise LobbyNotLaunchableError(exc.reason) from exc

    roster = await _curated_roster(session, lobby.ruleset_id)
    available_ai_build_count = available_auto_fill_build_count(seats=seats, curated_roster=roster)
    model_readiness = evaluate_lobby_rows(
        members=members,
        seats=seats,
        expected_player_count=ruleset.PLAYER_COUNT,
        available_ai_build_count=available_ai_build_count,
        now=now,
        stale_seconds=stale_seconds,
    )
    if not model_readiness.allowed:
        raise LobbyNotLaunchableError(model_readiness.reasons[0].value)

    # Resolve reserved AI builds (host pre-picks) and the empty AI seats.
    reserved: dict[int, str] = {}
    empty_indices: list[int] = []
    for seat in seats:
        if seat.seat_kind != LobbySeatKind.AI.value:
            continue
        if seat.agent_build_id is not None:
            reserved[seat.seat_index] = str(seat.agent_build_id)
        else:
            empty_indices.append(seat.seat_index)

    try:
        filled = autofill_empty_seats(
            lobby_seed=lobby.lobby_seed,
            empty_seat_indices=empty_indices,
            reserved_build_ids=reserved,
            curated_roster=roster,
        )
    except NotEnoughCuratedModelsError as exc:
        raise AutoFillPoolError(str(exc)) from exc

    game_seed = _derive_game_seed(lobby.lobby_seed)
    assigned = assign_roles(game_seed, ruleset)
    roles_by_index = {seat.seat_index: seat for seat in assigned}
    seats_by_index = {seat.seat_index: seat for seat in seats}
    if is_anonymous(lobby.identity_mode):
        # ``seat_permutation`` maps public game seat index -> source lobby slot.
        # The game seat index remains the public/replay index from RolesAssigned;
        # only the lobby slot supplying that public seat is shuffled.
        source_lobby_slot_by_public_index = seat_permutation(
            f"anonymous-launch:{lobby.lobby_seed}", ruleset.PLAYER_COUNT
        )
    else:
        source_lobby_slot_by_public_index = tuple(range(ruleset.PLAYER_COUNT))

    if funding_settings is None:
        from padrino.settings import get_settings

        funding_settings = get_settings()
    # Funding hold + game materialization are one savepoint. If an unexpected
    # post-reservation problem occurs, the hold rolls back before the route can
    # commit its ordinary admission-slot cleanup for the failed attempt.
    async with session.begin_nested():
        game_id = await _reserve_and_materialize_game(
            session,
            lobby=lobby,
            lobby_id=lobby_id,
            game_seed=game_seed,
            roles_by_index=roles_by_index,
            seats_by_index=seats_by_index,
            source_lobby_slot_by_public_index=source_lobby_slot_by_public_index,
            filled=filled,
            source_ip_hash=source_ip_hash,
            funding_quote_hash=funding_quote_hash,
            byok_max_estimate_micros=byok_max_estimate_micros,
            funding_settings=funding_settings,
            now=materialization_now,
        )
    return LaunchResult(game_id=game_id, created=True)


async def _reserve_and_materialize_game(
    session: AsyncSession,
    *,
    lobby: Lobby,
    lobby_id: uuid.UUID,
    game_seed: str,
    roles_by_index: dict[int, Seat],
    seats_by_index: dict[int, LobbySeat],
    source_lobby_slot_by_public_index: tuple[int, ...],
    filled: dict[int, str],
    source_ip_hash: str | None,
    funding_quote_hash: str | None,
    byok_max_estimate_micros: int | None,
    funding_settings: Settings,
    now: datetime,
) -> uuid.UUID:
    """Reserve funding before creating one game inside the caller's savepoint."""
    funding = await admit_lobby_funding_before_game(
        session,
        lobby=lobby,
        settings=funding_settings,
        funding_quote_hash=funding_quote_hash,
        byok_max_estimate_micros=byok_max_estimate_micros,
        now=now,
    )
    game = await games_repo.create(
        session,
        ruleset_id=lobby.ruleset_id,
        game_seed=game_seed,
        status="CREATED",
        rules_snapshot_json=lobby.rules_snapshot_json,
        rules_snapshot_hash=lobby.rules_snapshot_hash,
    )
    game.identity_mode = lobby.identity_mode
    game.ranked_source_ip_hash = source_ip_hash
    member_principal = await _member_principals(session, lobby_id)
    for public_seat_index, source_lobby_slot in enumerate(source_lobby_slot_by_public_index):
        seat = seats_by_index[source_lobby_slot]
        role_seat = roles_by_index[public_seat_index]
        if seat.seat_kind == LobbySeatKind.HUMAN.value:
            occupant = member_principal.get(seat.member_id) if seat.member_id else None
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=role_seat.public_player_id,
                    seat_index=role_seat.seat_index,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value,
                    occupant_principal_id=occupant,
                    role=role_seat.role.value,
                    faction=role_seat.faction.value,
                    alive=True,
                )
            )
            continue
        build_id = (
            str(seat.agent_build_id) if seat.agent_build_id is not None else filled[seat.seat_index]
        )
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=role_seat.public_player_id,
                seat_index=role_seat.seat_index,
                agent_build_id=uuid.UUID(build_id),
                seat_kind=SeatKind.AI.value,
                role=role_seat.role.value,
                faction=role_seat.faction.value,
                alive=True,
            )
        )
    lobby.game_id = game.id
    lobby.status = LobbyStatus.LAUNCHED.value
    await bind_funding_reservation_to_game(
        session,
        reservation=funding.reservation,
        game_id=game.id,
        now=now,
    )
    await session.flush()
    return game.id


async def _member_principals(
    session: AsyncSession, lobby_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID]:
    """Map ``member_id -> principal_id`` for a lobby's members."""
    stmt = select(LobbyMember.id, LobbyMember.principal_id).where(LobbyMember.lobby_id == lobby_id)
    rows = (await session.execute(stmt)).all()
    return {row[0]: row[1] for row in rows}


__all__ = [
    "AutoFillPoolError",
    "LaunchResult",
    "LobbyNotLaunchableError",
    "launch_lobby",
]
