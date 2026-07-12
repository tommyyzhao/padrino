"""Pure shaping for authenticated human GDPR export bundles.

The API/database shell fetches rows already scoped to one principal. This module
only normalizes those row projections into a deterministic JSON-ready shape and
strips fields that should not leave the server, such as consent IP hashes and
session hashes.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION: Literal["padrino.gdpr_human_export.v1"] = "padrino.gdpr_human_export.v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PrincipalExportInput(_FrozenModel):
    """Already-fetched principal profile fields for export shaping."""

    id: uuid.UUID
    kind: str
    display_name: str | None
    ranked_handle: str | None
    ranked_handle_normalized: str | None
    deleted_at: Any | None
    created_at: Any
    updated_at: Any


class SessionExportInput(_FrozenModel):
    """Already-fetched human-session row, including server-only hash input."""

    id: uuid.UUID
    principal_id: uuid.UUID
    session_hash: str
    kind: str
    issued_at: Any
    expires_at: Any
    revoked_at: Any | None
    last_seen_at: Any | None


class ConsentExportInput(_FrozenModel):
    """Already-fetched consent row, including server-only IP hash input."""

    id: uuid.UUID
    subject_principal_id: uuid.UUID
    document_kind: str
    document_version: str
    content_hash: str | None
    accepted_at: Any
    source_ip_hash: str | None


class ChatExportInput(_FrozenModel):
    """Already-fetched human-chat sidecar row."""

    id: uuid.UUID
    game_id: uuid.UUID
    sequence: int
    public_player_id: str
    raw_text: str | None
    cleaned_text: str | None
    redacted: bool
    created_at: Any


class HumanRatingExportInput(_FrozenModel):
    """Already-fetched current human-rating row."""

    id: uuid.UUID
    league_id: uuid.UUID
    human_player_id: str
    scope_type: str
    scope_value: str
    mu: float
    sigma: float
    conservative_score: float
    games: int
    last_game_at: Any | None
    quarantined_at: Any | None
    updated_at: Any


class HumanRatingEventExportInput(_FrozenModel):
    """Already-fetched per-game human-rating event row."""

    id: uuid.UUID
    league_id: uuid.UUID
    game_id: uuid.UUID
    human_player_id: str
    public_player_id: str | None
    scope_type: str
    scope_value: str
    before_mu: float
    before_sigma: float
    after_mu: float
    after_sigma: float
    created_at: Any


class OpenRouterConnectionExportInput(_FrozenModel):
    """Safe connection lifecycle metadata for a self-export."""

    id: uuid.UUID
    provider: str
    label: str | None
    display_suffix: str
    created_at: Any
    last_validated_at: Any | None
    revoked_at: Any | None


class WalletLedgerExportInput(_FrozenModel):
    """One immutable, secret-free promotional-wallet fact for self-export."""

    id: uuid.UUID
    entry_type: str
    amount_micros: int
    reason: str
    created_at: Any


class PrincipalExport(_FrozenModel):
    """Self profile included in the authenticated export."""

    id: uuid.UUID
    kind: str
    display_name: str | None
    ranked_handle: str | None
    ranked_handle_normalized: str | None
    deleted_at: Any | None
    created_at: Any
    updated_at: Any


class SessionExport(_FrozenModel):
    """Session metadata with impersonation-enabling hashes removed."""

    id: uuid.UUID
    principal_id: uuid.UUID
    kind: str
    issued_at: Any
    expires_at: Any
    revoked_at: Any | None
    last_seen_at: Any | None


class ConsentExport(_FrozenModel):
    """Consent history with request-source hashes removed."""

    id: uuid.UUID
    subject_principal_id: uuid.UUID
    document_kind: str
    document_version: str
    content_hash: str | None
    accepted_at: Any


class ChatExport(_FrozenModel):
    """Authored human-chat sidecar row, redacted rows carrying no text."""

    id: uuid.UUID
    game_id: uuid.UUID
    sequence: int
    public_player_id: str
    raw_text: str | None
    cleaned_text: str | None
    redacted: bool
    created_at: Any


class HumanRatingExport(_FrozenModel):
    """Current ranked human-rating row."""

    id: uuid.UUID
    league_id: uuid.UUID
    human_player_id: str
    scope_type: str
    scope_value: str
    mu: float
    sigma: float
    conservative_score: float
    games: int
    last_game_at: Any | None
    quarantined_at: Any | None
    updated_at: Any


class HumanRatingEventExport(_FrozenModel):
    """Per-game ranked human-rating history row."""

    id: uuid.UUID
    league_id: uuid.UUID
    game_id: uuid.UUID
    human_player_id: str
    public_player_id: str | None
    scope_type: str
    scope_value: str
    before_mu: float
    before_sigma: float
    after_mu: float
    after_sigma: float
    created_at: Any


class OpenRouterConnectionExport(_FrozenModel):
    """Connection metadata without envelope or management-plane material."""

    provider: str
    label: str | None
    display_suffix: str
    created_at: Any
    last_validated_at: Any | None
    revoked_at: Any | None


class WalletLedgerExport(_FrozenModel):
    """An immutable promotional-wallet entry without internal linkage keys."""

    entry_type: str
    amount_micros: int
    reason: str
    created_at: Any


class HumanDataExportBundle(_FrozenModel):
    """Deterministic authenticated export of one principal's human data."""

    schema_version: Literal["padrino.gdpr_human_export.v1"] = SCHEMA_VERSION
    principal: PrincipalExport
    sessions: tuple[SessionExport, ...]
    consents: tuple[ConsentExport, ...]
    chat_messages: tuple[ChatExport, ...]
    human_ratings: tuple[HumanRatingExport, ...]
    human_rating_events: tuple[HumanRatingEventExport, ...]
    openrouter_connections: tuple[OpenRouterConnectionExport, ...] = ()
    wallet_ledger_entries: tuple[WalletLedgerExport, ...] = ()


def assemble_human_data_export(
    *,
    principal: PrincipalExportInput | PrincipalExport,
    sessions: list[SessionExportInput] | tuple[SessionExportInput, ...],
    consents: list[ConsentExportInput] | tuple[ConsentExportInput, ...],
    chat_messages: list[ChatExportInput] | tuple[ChatExportInput, ...],
    human_ratings: list[HumanRatingExportInput] | tuple[HumanRatingExportInput, ...],
    human_rating_events: list[HumanRatingEventExportInput]
    | tuple[HumanRatingEventExportInput, ...],
    openrouter_connections: list[OpenRouterConnectionExportInput]
    | tuple[OpenRouterConnectionExportInput, ...] = (),
    wallet_ledger_entries: list[WalletLedgerExportInput] | tuple[WalletLedgerExportInput, ...] = (),
) -> HumanDataExportBundle:
    """Return a deterministic self-export bundle from already-scoped rows."""

    return HumanDataExportBundle(
        principal=PrincipalExport(
            id=principal.id,
            kind=principal.kind,
            display_name=principal.display_name,
            ranked_handle=principal.ranked_handle,
            ranked_handle_normalized=principal.ranked_handle_normalized,
            deleted_at=principal.deleted_at,
            created_at=principal.created_at,
            updated_at=principal.updated_at,
        ),
        sessions=tuple(_session(row) for row in sorted(sessions, key=_session_key)),
        consents=tuple(_consent(row) for row in sorted(consents, key=_consent_key)),
        chat_messages=tuple(_chat(row) for row in sorted(chat_messages, key=_chat_key)),
        human_ratings=tuple(_rating(row) for row in sorted(human_ratings, key=_rating_key)),
        human_rating_events=tuple(
            _rating_event(row) for row in sorted(human_rating_events, key=_rating_event_key)
        ),
        openrouter_connections=tuple(
            _openrouter_connection(row)
            for row in sorted(openrouter_connections, key=_openrouter_connection_key)
        ),
        wallet_ledger_entries=tuple(
            _wallet_ledger_entry(row)
            for row in sorted(wallet_ledger_entries, key=_wallet_ledger_entry_key)
        ),
    )


def _session(row: SessionExportInput) -> SessionExport:
    return SessionExport(
        id=row.id,
        principal_id=row.principal_id,
        kind=row.kind,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_seen_at=row.last_seen_at,
    )


def _consent(row: ConsentExportInput) -> ConsentExport:
    return ConsentExport(
        id=row.id,
        subject_principal_id=row.subject_principal_id,
        document_kind=row.document_kind,
        document_version=row.document_version,
        content_hash=row.content_hash,
        accepted_at=row.accepted_at,
    )


def _chat(row: ChatExportInput) -> ChatExport:
    return ChatExport(
        id=row.id,
        game_id=row.game_id,
        sequence=row.sequence,
        public_player_id=row.public_player_id,
        raw_text=None if row.redacted else row.raw_text,
        cleaned_text=None if row.redacted else row.cleaned_text,
        redacted=row.redacted,
        created_at=row.created_at,
    )


def _rating(row: HumanRatingExportInput) -> HumanRatingExport:
    return HumanRatingExport(
        id=row.id,
        league_id=row.league_id,
        human_player_id=row.human_player_id,
        scope_type=row.scope_type,
        scope_value=row.scope_value,
        mu=row.mu,
        sigma=row.sigma,
        conservative_score=row.conservative_score,
        games=row.games,
        last_game_at=row.last_game_at,
        quarantined_at=row.quarantined_at,
        updated_at=row.updated_at,
    )


def _rating_event(row: HumanRatingEventExportInput) -> HumanRatingEventExport:
    return HumanRatingEventExport(
        id=row.id,
        league_id=row.league_id,
        game_id=row.game_id,
        human_player_id=row.human_player_id,
        public_player_id=row.public_player_id,
        scope_type=row.scope_type,
        scope_value=row.scope_value,
        before_mu=row.before_mu,
        before_sigma=row.before_sigma,
        after_mu=row.after_mu,
        after_sigma=row.after_sigma,
        created_at=row.created_at,
    )


def _openrouter_connection(
    row: OpenRouterConnectionExportInput,
) -> OpenRouterConnectionExport:
    return OpenRouterConnectionExport(
        provider=row.provider,
        label=row.label,
        display_suffix=row.display_suffix,
        created_at=row.created_at,
        last_validated_at=row.last_validated_at,
        revoked_at=row.revoked_at,
    )


def _wallet_ledger_entry(row: WalletLedgerExportInput) -> WalletLedgerExport:
    return WalletLedgerExport(
        entry_type=row.entry_type,
        amount_micros=row.amount_micros,
        reason=row.reason,
        created_at=row.created_at,
    )


def _dt_key(value: Any | None) -> str:
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _session_key(row: SessionExportInput) -> tuple[str, str]:
    return (_dt_key(row.issued_at), str(row.id))


def _consent_key(row: ConsentExportInput) -> tuple[str, str, str]:
    return (_dt_key(row.accepted_at), row.document_kind, str(row.id))


def _chat_key(row: ChatExportInput) -> tuple[str, int, str]:
    return (str(row.game_id), row.sequence, str(row.id))


def _rating_key(row: HumanRatingExportInput) -> tuple[str, str, str, str]:
    return (str(row.league_id), row.scope_type, row.scope_value, str(row.id))


def _rating_event_key(row: HumanRatingEventExportInput) -> tuple[str, str, str, str, str]:
    return (
        _dt_key(row.created_at),
        str(row.game_id),
        row.scope_type,
        row.scope_value,
        str(row.id),
    )


def _openrouter_connection_key(row: OpenRouterConnectionExportInput) -> tuple[str, str, str]:
    return (row.provider, _dt_key(row.created_at), str(row.id))


def _wallet_ledger_entry_key(row: WalletLedgerExportInput) -> tuple[str, str]:
    return (_dt_key(row.created_at), str(row.id))


__all__ = [
    "SCHEMA_VERSION",
    "ChatExport",
    "ChatExportInput",
    "ConsentExport",
    "ConsentExportInput",
    "HumanDataExportBundle",
    "HumanRatingEventExport",
    "HumanRatingEventExportInput",
    "HumanRatingExport",
    "HumanRatingExportInput",
    "OpenRouterConnectionExport",
    "OpenRouterConnectionExportInput",
    "PrincipalExport",
    "PrincipalExportInput",
    "SessionExport",
    "SessionExportInput",
    "WalletLedgerExport",
    "WalletLedgerExportInput",
    "assemble_human_data_export",
]
