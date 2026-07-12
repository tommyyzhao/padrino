"""CRUD helpers for private friend lobbies (US-147).

A lobby configures one human-multiplayer game (ruleset/size, identity mode, theme
pack, bot pre-pick vs auto-fill, stakes pinned CASUAL) and tracks its membership
and pre-launch seat layout. The host creates it; invited friends join (US-148);
empty seats are filled deterministically at launch (US-149).

Seeds and IDs are produced in the impure API shell and passed in — this module
performs no clock reads, no ``secrets``, and no ``random`` (those live in the
api/runner layer, like every other repository).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LobbySeatKind
from padrino.core.lobby.seating import (
    LobbySeatPlan,
    LobbySeatPlanEntry,
    claim_lowest_available_ai_seat,
)
from padrino.core.lobby.seating import (
    release_member_seat as plan_release_member_seat,
)
from padrino.db.models import Lobby, LobbyMember, LobbySeat, Principal
from padrino.db.rules_snapshots import (
    RulesSnapshotJson,
    ensure_lobby_rules_snapshot_mutable,
    resolve_rules_snapshot_pair,
    validate_rules_snapshot_pair,
)


async def create_lobby(
    session: AsyncSession,
    *,
    ruleset_id: str,
    identity_mode: str,
    theme_pack_id: str | None,
    lobby_seed: str,
    invite_token: str,
    integrity_acknowledged: bool,
    host_principal_id: uuid.UUID,
    league_id: uuid.UUID,
    now: datetime,
    human_seat_capacity: int | None = None,
    rules_snapshot_json: RulesSnapshotJson | None = None,
    rules_snapshot_hash: str | None = None,
) -> Lobby:
    """Create an OPEN, CASUAL lobby owned by ``host_principal_id``.

    Stakes are pinned to ``CASUAL`` and status to ``OPEN`` (the model defaults);
    ``league_id`` must be the dormant Humans-Included league so the lobby never
    references a scientific league. ``invite_token`` is an opaque shareable
    address minted in the api shell (US-148); ``secrets`` never lives here.
    """
    snapshot_json, snapshot_hash = resolve_rules_snapshot_pair(
        ruleset_id=ruleset_id,
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )
    obj = Lobby(
        ruleset_id=ruleset_id,
        identity_mode=identity_mode,
        theme_pack_id=theme_pack_id,
        lobby_seed=lobby_seed,
        invite_token=invite_token,
        integrity_acknowledged=integrity_acknowledged,
        human_seat_capacity=human_seat_capacity,
        host_principal_id=host_principal_id,
        league_id=league_id,
        created_at=now,
        updated_at=now,
        rules_snapshot_json=snapshot_json,
        rules_snapshot_hash=snapshot_hash,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_lobby(session: AsyncSession, lobby_id: uuid.UUID) -> Lobby | None:
    return await session.get(Lobby, lobby_id)


async def get_lobby_by_invite_token(session: AsyncSession, invite_token: str) -> Lobby | None:
    result = await session.execute(select(Lobby).where(Lobby.invite_token == invite_token))
    return result.scalar_one_or_none()


async def set_lobby_status(
    session: AsyncSession, *, lobby_id: uuid.UUID, status: str, now: datetime
) -> None:
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        return
    lobby.status = status
    lobby.updated_at = now


async def touch_lobby(session: AsyncSession, *, lobby_id: uuid.UUID, now: datetime) -> None:
    """Mark the lobby active (resets the idle auto-cancel clock)."""
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        return
    lobby.updated_at = now


async def update_rules_snapshot(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    rules_snapshot_json: RulesSnapshotJson | None,
    rules_snapshot_hash: str | None,
) -> Lobby | None:
    """Replace a pre-launch lobby rules snapshot after validating the hash pair."""
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        return None
    ensure_lobby_rules_snapshot_mutable(lobby.status)
    snapshot_json, snapshot_hash = validate_rules_snapshot_pair(
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )
    lobby.rules_snapshot_json = snapshot_json
    lobby.rules_snapshot_hash = snapshot_hash
    await session.flush()
    return lobby


async def update_configuration(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    ruleset_id: str,
    identity_mode: str,
    theme_pack_id: str | None,
    human_seat_capacity: int,
    rules_snapshot_json: RulesSnapshotJson | None,
    rules_snapshot_hash: str | None,
    now: datetime,
) -> Lobby | None:
    """Replace pre-launch mutable lobby configuration."""
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        return None
    ensure_lobby_rules_snapshot_mutable(lobby.status)
    snapshot_json, snapshot_hash = validate_rules_snapshot_pair(
        rules_snapshot_json=rules_snapshot_json,
        rules_snapshot_hash=rules_snapshot_hash,
    )
    lobby.ruleset_id = ruleset_id
    lobby.identity_mode = identity_mode
    lobby.theme_pack_id = theme_pack_id
    lobby.human_seat_capacity = human_seat_capacity
    lobby.rules_snapshot_json = snapshot_json
    lobby.rules_snapshot_hash = snapshot_hash
    lobby.updated_at = now
    await session.flush()
    return lobby


async def update_theme(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    theme_pack_id: str | None,
    now: datetime,
) -> Lobby | None:
    """Update the safe cosmetic lobby theme while the lobby is still open."""
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        return None
    lobby.theme_pack_id = theme_pack_id
    lobby.updated_at = now
    await session.flush()
    return lobby


async def add_member(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    principal_id: uuid.UUID,
    is_host: bool,
    now: datetime,
) -> LobbyMember:
    obj = LobbyMember(
        lobby_id=lobby_id,
        principal_id=principal_id,
        is_host=is_host,
        joined_at=now,
    )
    session.add(obj)
    await session.flush()
    return obj


async def list_members(session: AsyncSession, lobby_id: uuid.UUID) -> list[LobbyMember]:
    result = await session.execute(
        select(LobbyMember)
        .where(LobbyMember.lobby_id == lobby_id)
        .order_by(LobbyMember.joined_at, LobbyMember.id)
    )
    return list(result.scalars())


async def list_member_display_names(
    session: AsyncSession, lobby_id: uuid.UUID
) -> dict[uuid.UUID, str | None]:
    """Return ``{lobby_member_id: display_name}`` for player-facing roster labels."""
    result = await session.execute(
        select(LobbyMember.id, Principal.display_name)
        .join(Principal, Principal.id == LobbyMember.principal_id)
        .where(LobbyMember.lobby_id == lobby_id)
    )
    return {row[0]: row[1] for row in result.all()}


async def get_member(
    session: AsyncSession, *, lobby_id: uuid.UUID, principal_id: uuid.UUID
) -> LobbyMember | None:
    result = await session.execute(
        select(LobbyMember).where(
            LobbyMember.lobby_id == lobby_id,
            LobbyMember.principal_id == principal_id,
        )
    )
    return result.scalar_one_or_none()


async def get_member_by_id(session: AsyncSession, member_id: uuid.UUID) -> LobbyMember | None:
    return await session.get(LobbyMember, member_id)


async def set_member_ready(session: AsyncSession, *, member_id: uuid.UUID, ready: bool) -> None:
    member = await session.get(LobbyMember, member_id)
    if member is None:
        return
    member.ready = ready


async def touch_member_presence(
    session: AsyncSession, *, member_id: uuid.UUID, now: datetime
) -> None:
    member = await session.get(LobbyMember, member_id)
    if member is None:
        return
    member.last_seen_at = now


async def remove_member(session: AsyncSession, *, member_id: uuid.UUID) -> None:
    member = await session.get(LobbyMember, member_id)
    if member is None:
        return
    await session.delete(member)
    await session.flush()


async def set_host(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    new_host_member_id: uuid.UUID,
    new_host_principal_id: uuid.UUID,
    now: datetime,
) -> None:
    """Transfer host: clear ``is_host`` on every member, set it on the new host."""
    for member in await list_members(session, lobby_id):
        member.is_host = member.id == new_host_member_id
    lobby = await session.get(Lobby, lobby_id)
    if lobby is not None:
        lobby.host_principal_id = new_host_principal_id
        lobby.updated_at = now


async def add_seat(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    seat_index: int,
    seat_kind: LobbySeatKind,
    member_id: uuid.UUID | None = None,
    agent_build_id: uuid.UUID | None = None,
) -> LobbySeat:
    obj = LobbySeat(
        lobby_id=lobby_id,
        seat_index=seat_index,
        seat_kind=seat_kind.value,
        member_id=member_id,
        agent_build_id=agent_build_id,
    )
    session.add(obj)
    await session.flush()
    return obj


async def claim_member_seat(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    member_id: uuid.UUID,
) -> LobbySeat:
    """Atomically claim the lowest available AI lobby seat for ``member_id``.

    The caller owns the transaction. Seat rows are selected ``FOR UPDATE`` where
    supported so concurrent last-seat joins serialize on Postgres; SQLite ignores
    the clause and still runs the same pure planner under the transaction.
    """
    lobby = await session.get(Lobby, lobby_id)
    human_seat_capacity = lobby.human_seat_capacity if lobby is not None else None
    seats = await _locked_seats(session, lobby_id)
    plan = claim_lowest_available_ai_seat(
        _seat_plan_entries(seats),
        joining_member_id=str(member_id),
        human_seat_capacity=human_seat_capacity,
    )
    await _apply_seat_plan(session, seats=seats, plan=plan)
    claimed = next(seat for seat in seats if seat.member_id == member_id)
    return claimed


async def release_member_seat(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    member_id: uuid.UUID,
) -> None:
    """Atomically release ``member_id`` back to its configured AI lobby seat."""
    seats = await _locked_seats(session, lobby_id)
    plan = plan_release_member_seat(_seat_plan_entries(seats), departing_member_id=str(member_id))
    await _apply_seat_plan(session, seats=seats, plan=plan)


async def list_seats(session: AsyncSession, lobby_id: uuid.UUID) -> list[LobbySeat]:
    result = await session.execute(
        select(LobbySeat).where(LobbySeat.lobby_id == lobby_id).order_by(LobbySeat.seat_index)
    )
    return list(result.scalars())


async def replace_seats(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
    seats: list[LobbySeatPlanEntry],
) -> None:
    """Replace every pre-launch seat row for a host-only lobby reconfiguration."""
    await session.execute(delete(LobbySeat).where(LobbySeat.lobby_id == lobby_id))
    for seat in seats:
        session.add(
            LobbySeat(
                lobby_id=lobby_id,
                seat_index=seat.seat_index,
                seat_kind=LobbySeatKind(seat.seat_kind).value,
                member_id=uuid.UUID(seat.member_id) if seat.member_id is not None else None,
                agent_build_id=(
                    uuid.UUID(seat.agent_build_id) if seat.agent_build_id is not None else None
                ),
            )
        )
    await session.flush()


async def _locked_seats(session: AsyncSession, lobby_id: uuid.UUID) -> list[LobbySeat]:
    result = await session.execute(
        select(LobbySeat)
        .where(LobbySeat.lobby_id == lobby_id)
        .order_by(LobbySeat.seat_index)
        .with_for_update()
    )
    return list(result.scalars())


def _seat_plan_entries(seats: list[LobbySeat]) -> LobbySeatPlan:
    return tuple(
        LobbySeatPlanEntry(
            seat_index=seat.seat_index,
            seat_kind=seat.seat_kind,
            member_id=str(seat.member_id) if seat.member_id is not None else None,
            agent_build_id=str(seat.agent_build_id) if seat.agent_build_id is not None else None,
        )
        for seat in seats
    )


async def _apply_seat_plan(
    session: AsyncSession,
    *,
    seats: list[LobbySeat],
    plan: LobbySeatPlan,
) -> None:
    rows_by_index = {seat.seat_index: seat for seat in seats}
    for entry in plan:
        row = rows_by_index[entry.seat_index]
        row.seat_kind = LobbySeatKind(entry.seat_kind).value
        row.member_id = uuid.UUID(entry.member_id) if entry.member_id is not None else None
        row.agent_build_id = (
            uuid.UUID(entry.agent_build_id) if entry.agent_build_id is not None else None
        )

    await session.flush()


__all__ = [
    "add_member",
    "add_seat",
    "claim_member_seat",
    "create_lobby",
    "get_lobby",
    "get_lobby_by_invite_token",
    "get_member",
    "get_member_by_id",
    "list_member_display_names",
    "list_members",
    "list_seats",
    "release_member_seat",
    "remove_member",
    "replace_seats",
    "set_host",
    "set_lobby_status",
    "set_member_ready",
    "touch_lobby",
    "touch_member_presence",
    "update_configuration",
    "update_rules_snapshot",
    "update_theme",
]
