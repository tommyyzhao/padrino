"""Private BYOK credential assembly for launched human-lane games (US-356).

This impure seam is the only game-runtime path that decrypts a player-owned
OpenRouter credential. Lobby snapshots retain the host ownership reference and
the durable call attribution retains only its UUID; the plaintext is handed
directly to an in-memory adapter and is never projected into events, prompts,
or persistence models.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.observation_privacy import is_anonymous
from padrino.core.observations import Observation
from padrino.core.seating import seat_permutation
from padrino.db.models import GameSeat, Lobby, LobbySeat, ProviderCredential
from padrino.llm.adapter import AdapterResult, LlmAdapter, ProviderNoContestError
from padrino.openrouter.credentials import (
    CredentialAuthenticationError,
    CredentialEncryptionUnavailable,
    CredentialEnvelopeService,
    credential_key_ring_from_settings,
)
from padrino.openrouter.lobby_model_selection import FrozenLobbyModelSelection
from padrino.settings import Settings

_OPENROUTER_PROVIDER = "openrouter"
_BYOK_FAILURE_STATUSES = frozenset({"provider_error", "primary_failed", "both_failed", "exhausted"})


class ByokRuntimeError(RuntimeError):
    """A safe, stable BYOK assembly failure without provider diagnostics."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, repr=False)
class ByokRuntimeBindings:
    """One host credential bound to the AI seats of a launched BYOK table."""

    credential_id: uuid.UUID
    api_key: str = field(repr=False)
    public_player_ids: tuple[str, ...]

    def secrets_by_seat(self) -> Mapping[str, str]:
        """Return the short-lived adapter-only plaintext binding."""
        return MappingProxyType(dict.fromkeys(self.public_player_ids, self.api_key))

    def credential_ids_by_seat(self) -> Mapping[str, uuid.UUID]:
        """Return durable, non-secret call attribution for each BYOK seat."""
        return MappingProxyType(dict.fromkeys(self.public_player_ids, self.credential_id))


class ByokProviderFailureAdapter:
    """Convert BYOK provider failures into a failed/no-contest game outcome."""

    __slots__ = ("_byok_seats", "_inner")

    def __init__(self, inner: LlmAdapter, *, byok_public_player_ids: Sequence[str]) -> None:
        self._inner = inner
        self._byok_seats = frozenset(byok_public_player_ids)

    async def complete(self, observation: Observation) -> AdapterResult:
        result = await self._inner.complete(observation)
        player_id = observation.you.player_id
        if player_id in self._byok_seats and result.status in _BYOK_FAILURE_STATUSES:
            raise ProviderNoContestError(reason="byok_provider_failure", result=result)
        return result


async def validate_byok_lobby_credentials(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> None:
    """Fail launch validation unless the frozen host BYOK credential is active.

    Validation intentionally reads only the lobby host's exact provider row;
    invited players' credentials are neither selected nor inspected.
    """
    lobby, lobby_seats, game_seats = await _lobby_context(session, game_id=game_id)
    byok_public_ids = _byok_public_player_ids(
        lobby=lobby,
        lobby_seats=lobby_seats,
        game_seats=game_seats,
    )
    if not byok_public_ids:
        return
    if await _active_host_credential(session, lobby.host_principal_id) is None:
        raise ByokRuntimeError("byok_credential_unavailable")


async def validate_byok_lobby_credentials_for_lobby(
    session: AsyncSession,
    *,
    lobby_id: uuid.UUID,
) -> None:
    """Validate BYOK ownership and active credential before a game is created."""
    lobby = await session.get(Lobby, lobby_id)
    if lobby is None:
        raise ByokRuntimeError("byok_lobby_not_found")
    seats = list(
        (
            await session.execute(
                select(LobbySeat)
                .where(LobbySeat.lobby_id == lobby_id)
                .order_by(LobbySeat.seat_index)
            )
        ).scalars()
    )
    if not _validate_byok_lobby_selections(lobby, seats):
        return
    if await _active_host_credential(session, lobby.host_principal_id) is None:
        raise ByokRuntimeError("byok_credential_unavailable")


async def resolve_byok_runtime_bindings(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    settings: Settings,
) -> ByokRuntimeBindings | None:
    """Decrypt one active host envelope only while an AI adapter is assembled."""
    lobby, lobby_seats, game_seats = await _lobby_context(session, game_id=game_id)
    byok_public_ids = _byok_public_player_ids(
        lobby=lobby,
        lobby_seats=lobby_seats,
        game_seats=game_seats,
    )
    if not byok_public_ids:
        return None
    credential = await _active_host_credential(session, lobby.host_principal_id)
    if credential is None:
        raise ByokRuntimeError("byok_credential_unavailable")
    try:
        service = CredentialEnvelopeService(credential_key_ring_from_settings(settings))
        api_key = service.decrypt_provider_credential(credential)
    except (CredentialAuthenticationError, CredentialEncryptionUnavailable):
        raise ByokRuntimeError("byok_credential_unavailable") from None
    return ByokRuntimeBindings(
        credential_id=credential.id,
        api_key=api_key,
        public_player_ids=byok_public_ids,
    )


async def _lobby_context(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> tuple[Lobby, list[LobbySeat], list[GameSeat]]:
    lobby = await session.scalar(select(Lobby).where(Lobby.game_id == game_id).limit(1))
    if lobby is None:
        raise ByokRuntimeError("byok_lobby_not_found")
    lobby_seats = list(
        (
            await session.execute(
                select(LobbySeat)
                .where(LobbySeat.lobby_id == lobby.id)
                .order_by(LobbySeat.seat_index)
            )
        ).scalars()
    )
    game_seats = list(
        (
            await session.execute(
                select(GameSeat).where(GameSeat.game_id == game_id).order_by(GameSeat.seat_index)
            )
        ).scalars()
    )
    return lobby, lobby_seats, game_seats


def _byok_public_player_ids(
    *,
    lobby: Lobby,
    lobby_seats: Sequence[LobbySeat],
    game_seats: Sequence[GameSeat],
) -> tuple[str, ...]:
    """Validate one host-funded BYOK table and return its public AI seat ids."""
    lobby_by_index = {seat.seat_index: seat for seat in lobby_seats}
    if len(lobby_by_index) != len(lobby_seats):
        raise ByokRuntimeError("byok_lobby_snapshot_invalid")
    source_indexes = (
        seat_permutation(f"anonymous-launch:{lobby.lobby_seed}", len(game_seats))
        if is_anonymous(lobby.identity_mode)
        else tuple(range(len(game_seats)))
    )
    selected: list[tuple[GameSeat, FrozenLobbyModelSelection]] = []
    for game_seat in game_seats:
        if game_seat.seat_kind != "AI":
            continue
        try:
            source_index = source_indexes[game_seat.seat_index]
            source = lobby_by_index[source_index]
        except (IndexError, KeyError):
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
        if source.model_selection is None:
            continue
        try:
            selection = FrozenLobbyModelSelection.model_validate(source.model_selection)
        except ValueError:
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
        if selection.funding_source == "byok":
            selected.append((game_seat, selection))

    if not selected:
        return ()
    expected_owner_ref = f"principal:{lobby.host_principal_id}"
    for game_seat in game_seats:
        if game_seat.seat_kind != "AI":
            continue
        try:
            source = lobby_by_index[source_indexes[game_seat.seat_index]]
        except (IndexError, KeyError):
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
        if source.model_selection is None:
            raise ByokRuntimeError("byok_funding_source_mixed")
        try:
            selection = FrozenLobbyModelSelection.model_validate(source.model_selection)
        except ValueError:
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
        if selection.funding_source != "byok":
            raise ByokRuntimeError("byok_funding_source_mixed")
        if selection.funding_owner_ref != expected_owner_ref:
            raise ByokRuntimeError("byok_credential_owner_mismatch")
    return tuple(seat.public_player_id for seat, _selection in selected)


def _validate_byok_lobby_selections(lobby: Lobby, seats: Sequence[LobbySeat]) -> bool:
    """Return whether all configured AI slots form one host-owned BYOK table."""
    parsed: list[FrozenLobbyModelSelection] = []
    for seat in seats:
        if seat.seat_kind != "AI":
            continue
        if seat.model_selection is None:
            continue
        try:
            parsed.append(FrozenLobbyModelSelection.model_validate(seat.model_selection))
        except ValueError:
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
    if not any(selection.funding_source == "byok" for selection in parsed):
        return False
    expected_owner_ref = f"principal:{lobby.host_principal_id}"
    for seat in seats:
        if seat.seat_kind != "AI":
            continue
        if seat.model_selection is None:
            raise ByokRuntimeError("byok_funding_source_mixed")
        try:
            selection = FrozenLobbyModelSelection.model_validate(seat.model_selection)
        except ValueError:
            raise ByokRuntimeError("byok_lobby_snapshot_invalid") from None
        if selection.funding_source != "byok":
            raise ByokRuntimeError("byok_funding_source_mixed")
        if selection.funding_owner_ref != expected_owner_ref:
            raise ByokRuntimeError("byok_credential_owner_mismatch")
    return True


async def _active_host_credential(
    session: AsyncSession,
    principal_id: uuid.UUID,
) -> ProviderCredential | None:
    credential = await session.scalar(
        select(ProviderCredential)
        .where(
            ProviderCredential.principal_id == principal_id,
            ProviderCredential.provider == _OPENROUTER_PROVIDER,
            ProviderCredential.revoked_at.is_(None),
        )
        .limit(1)
    )
    return credential


__all__ = [
    "ByokProviderFailureAdapter",
    "ByokRuntimeBindings",
    "ByokRuntimeError",
    "resolve_byok_runtime_bindings",
    "validate_byok_lobby_credentials",
    "validate_byok_lobby_credentials_for_lobby",
]
