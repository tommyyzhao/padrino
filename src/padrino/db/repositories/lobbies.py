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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LobbySeatKind
from padrino.db.models import Lobby, LobbyMember, LobbySeat


async def create_lobby(
    session: AsyncSession,
    *,
    ruleset_id: str,
    identity_mode: str,
    theme_pack_id: str | None,
    lobby_seed: str,
    invite_token: str,
    host_principal_id: uuid.UUID,
    league_id: uuid.UUID,
    now: datetime,
) -> Lobby:
    """Create an OPEN, CASUAL lobby owned by ``host_principal_id``.

    Stakes are pinned to ``CASUAL`` and status to ``OPEN`` (the model defaults);
    ``league_id`` must be the dormant Humans-Included league so the lobby never
    references a scientific league. ``invite_token`` is an opaque shareable
    address minted in the api shell (US-148); ``secrets`` never lives here.
    """
    obj = Lobby(
        ruleset_id=ruleset_id,
        identity_mode=identity_mode,
        theme_pack_id=theme_pack_id,
        lobby_seed=lobby_seed,
        invite_token=invite_token,
        host_principal_id=host_principal_id,
        league_id=league_id,
        created_at=now,
        updated_at=now,
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


async def list_seats(session: AsyncSession, lobby_id: uuid.UUID) -> list[LobbySeat]:
    result = await session.execute(
        select(LobbySeat).where(LobbySeat.lobby_id == lobby_id).order_by(LobbySeat.seat_index)
    )
    return list(result.scalars())


__all__ = [
    "add_member",
    "add_seat",
    "create_lobby",
    "get_lobby",
    "get_lobby_by_invite_token",
    "get_member",
    "get_member_by_id",
    "list_members",
    "list_seats",
    "remove_member",
    "set_host",
    "set_lobby_status",
    "set_member_ready",
    "touch_lobby",
    "touch_member_presence",
]
