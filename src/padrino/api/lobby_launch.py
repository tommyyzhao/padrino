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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import LobbySeatKind, LobbyStatus, SeatKind
from padrino.core.lobby.autofill import (
    NotEnoughCuratedModelsError,
    autofill_empty_seats,
)
from padrino.core.rulesets import get_ruleset
from padrino.db.models import AgentBuild, GameSeat, LobbyMember
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import lobbies as lobbies_repo


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

    v1: every ACTIVE agent build whose prompt targets this ruleset, ordered
    deterministically (created_at, id). A dedicated ``human_eligible`` flag /
    curated pool table is a later-story concern (US-151); the active roster is
    the curated pool for now.
    """
    from padrino.db.models import PromptVersion

    stmt = (
        select(AgentBuild.id)
        .join(PromptVersion, AgentBuild.prompt_version_id == PromptVersion.id)
        .where(AgentBuild.active.is_(True))
        .where(PromptVersion.ruleset_id == ruleset_id)
        .order_by(AgentBuild.created_at, AgentBuild.id)
    )
    return [str(bid) for bid in (await session.execute(stmt)).scalars()]


async def launch_lobby(session: AsyncSession, *, lobby_id: uuid.UUID) -> LaunchResult:
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

    ruleset = get_ruleset(lobby.ruleset_id)
    seats = await lobbies_repo.list_seats(session, lobby_id)
    if len(seats) != ruleset.PLAYER_COUNT:
        raise LobbyNotLaunchableError("seat_count_mismatch")

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

    roster = await _curated_roster(session, lobby.ruleset_id)
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

    game = await games_repo.create(
        session,
        ruleset_id=lobby.ruleset_id,
        game_seed=game_seed,
        status="CREATED",
    )
    game.identity_mode = lobby.identity_mode

    member_principal = await _member_principals(session, lobby_id)

    for seat in sorted(seats, key=lambda s: s.seat_index):
        role_seat = roles_by_index[seat.seat_index]
        if seat.seat_kind == LobbySeatKind.HUMAN.value:
            occupant = member_principal.get(seat.member_id) if seat.member_id else None
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=role_seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=None,
                    seat_kind=SeatKind.HUMAN.value,
                    occupant_principal_id=occupant,
                    role=role_seat.role.value,
                    faction=role_seat.faction.value,
                    alive=True,
                )
            )
        else:
            build_id = (
                str(seat.agent_build_id)
                if seat.agent_build_id is not None
                else filled[seat.seat_index]
            )
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=role_seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=uuid.UUID(build_id),
                    seat_kind=SeatKind.AI.value,
                    role=role_seat.role.value,
                    faction=role_seat.faction.value,
                    alive=True,
                )
            )

    lobby.game_id = game.id
    lobby.status = LobbyStatus.LAUNCHED.value
    await session.flush()
    return LaunchResult(game_id=game.id, created=True)


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
