"""Signed game-export bundle (US-061).

Produces a single JSON artifact for one completed game so an operator can
publish the result to a federated leaderboard or share it with a colleague
and prove it was not tampered with.

The bundle carries:

- League / gauntlet / game identifiers, ruleset id, seed, terminal result.
- One entry per seat in :class:`AgentBuildInfo` resolved to ``display_name``
  + ``prompt_version`` + non-secret model metadata. ``ModelProvider.auth_secret_ref``
  is never copied — only ``provider.name`` lands in the bundle.
- One entry per seat in :class:`GameSeatInfo` with role / faction / alive flag.
- The full event log in canonical order as :class:`EventEnvelope` rows.
- ``tip_hash`` — the final ``event_hash`` in the chain.
- Optional Ed25519 signature over the canonical-JSON bytes of the bundle
  (excluding the signature itself), keyed by ``signer_fingerprint``.

Serialization is via :func:`padrino.core.engine.canonical_json.canonical_dumps`
so two exports of the same game are byte-identical and the signature is
deterministic. Verification re-canonicalizes and re-checks the chain via
:func:`padrino.core.engine.replay.replay_event_log`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.event_log import StoredEvent
from padrino.core.engine.replay import ReplayHashMismatchError, replay_event_log
from padrino.db.models import GameEvent
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    events as events_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)

SCHEMA_VERSION: Final[str] = "padrino.export.v1"

# Keys that must never appear inside an exported event payload. The engine
# never emits these on its own — this is defense in depth: it catches a
# future bug that leaks model provenance or operator secrets into the event
# log, and it gives downstream verifiers a single guard to assert against.
# Note: ``role`` and ``faction`` are intentionally NOT in this set — they
# legitimately appear under ``RolesAssigned.payload.assignments`` and inside
# ``PlayerEliminated.payload``; their privacy is a runner / ranked-observation
# concern handled by :mod:`padrino.core.observation_privacy`.
EXPORT_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "agent_build_id",
        "model_id",
        "model_name",
        "provider",
        "provider_name",
        "rating",
        "ratings",
        "win_rate",
        "win_rates",
        "elo",
        "openskill_mu",
        "openskill_sigma",
        "gauntlet_clone_index",
        "clone_index",
        "auth_secret_ref",
        "api_key",
        "authorization",
    }
)


class ExportError(ValueError):
    """Base error for export-bundle failures."""


class GameNotExportable(ExportError):
    """Raised when a game does not exist or is not in a terminal state."""


class BundlePayloadUnsafeError(ExportError):
    """Raised when an event payload would leak forbidden provenance / secrets."""


class AgentBuildInfo(BaseModel):
    """One seat's agent-build provenance, no secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_player_id: str
    seat_index: int
    display_name: str
    prompt_version: str
    model_provider: str
    model_name: str
    model_version: str | None


class GameSeatInfo(BaseModel):
    """One seat's terminal role / faction / liveness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    public_player_id: str
    seat_index: int
    role: str
    faction: str
    alive: bool
    death_phase: str | None


class EventEnvelope(BaseModel):
    """One persisted event in the bundle, hash-chain envelope included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    event_type: str
    phase: str
    visibility: str
    actor_player_id: str | None
    payload: dict[str, Any]
    prev_event_hash: str
    event_hash: str


class GameBundle(BaseModel):
    """The full export artifact for one completed game."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    ruleset_id: str
    league_id: str | None
    gauntlet_id: str | None
    game_id: str
    seed: str
    terminal_result: dict[str, Any] | None
    tip_hash: str
    agent_builds: list[AgentBuildInfo]
    game_seats: list[GameSeatInfo]
    events: list[EventEnvelope]
    signer_fingerprint: str | None = None
    sig: str | None = None


# --------------------------------------------------------------------------- #
# Canonicalization + signing
# --------------------------------------------------------------------------- #


def canonical_bundle_bytes(bundle: GameBundle) -> bytes:
    """Return the canonical-JSON byte representation used for signing.

    The ``sig`` field is excluded so a signature can be attached after the
    fact without changing the signed bytes. ``signer_fingerprint`` IS
    included so the bundle bound to a particular signer cannot be re-signed
    under a different identity without invalidating the signature.
    """
    payload = bundle.model_dump(mode="json", exclude={"sig"})
    return canonical_dumps(payload)


class Ed25519Signer:
    """Convenience wrapper around an :class:`Ed25519PrivateKey`."""

    __slots__ = ("_sk",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._sk = private_key

    @classmethod
    def generate(cls) -> Ed25519Signer:
        """Create a fresh signer with a randomly generated key pair."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed_b64(cls, seed_b64: str) -> Ed25519Signer:
        """Build a signer from a base64-encoded 32-byte seed."""
        try:
            seed = base64.urlsafe_b64decode(seed_b64.encode("ascii"))
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ExportError(f"invalid base64 Ed25519 seed: {exc}") from exc
        if len(seed) != 32:
            raise ExportError(f"Ed25519 seed must decode to 32 bytes, got {len(seed)}")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_env(cls, env_var: str) -> Ed25519Signer:
        """Read a base64 seed from ``os.environ[env_var]`` and build a signer."""
        raw = os.environ.get(env_var)
        if not raw:
            raise ExportError(f"env var {env_var!r} is unset or empty")
        return cls.from_seed_b64(raw)

    @property
    def fingerprint(self) -> str:
        """SHA-256 fingerprint of the raw public key (first 32 hex chars)."""
        return _fingerprint(self.public_key_bytes())

    def public_key_bytes(self) -> bytes:
        """Raw 32-byte public key."""
        return self._sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_b64(self) -> str:
        """Base64 (urlsafe, padded) encoding of the raw public key."""
        return base64.urlsafe_b64encode(self.public_key_bytes()).decode("ascii")

    def sign(self, data: bytes) -> str:
        """Return the base64 (urlsafe, padded) Ed25519 signature over ``data``."""
        return base64.urlsafe_b64encode(self._sk.sign(data)).decode("ascii")


def _fingerprint(public_key_bytes: bytes) -> str:
    return hashlib.sha256(public_key_bytes).hexdigest()[:32]


def verify_bundle_signature(bundle: GameBundle, public_key_b64: str) -> bool:
    """Return True iff ``bundle.sig`` is a valid Ed25519 signature for ``public_key_b64``.

    Recomputes :func:`canonical_bundle_bytes` and verifies the embedded
    signature. Returns False when no signature is attached or when the key
    does not match the bundle's ``signer_fingerprint`` — callers that need to
    distinguish absent-vs-mismatched should inspect ``bundle.sig`` and
    ``bundle.signer_fingerprint`` directly.
    """
    if bundle.sig is None:
        return False
    try:
        pk_bytes = base64.urlsafe_b64decode(public_key_b64.encode("ascii"))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    if len(pk_bytes) != 32:
        return False
    if bundle.signer_fingerprint is not None and bundle.signer_fingerprint != _fingerprint(
        pk_bytes
    ):
        return False
    try:
        sig_bytes = base64.urlsafe_b64decode(bundle.sig.encode("ascii"))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    pk = Ed25519PublicKey.from_public_bytes(pk_bytes)
    payload = canonical_bundle_bytes(bundle)
    try:
        pk.verify(sig_bytes, payload)
    except InvalidSignature:
        return False
    return True


# --------------------------------------------------------------------------- #
# Safety guard over event payloads
# --------------------------------------------------------------------------- #


def assert_bundle_payload_safe(events: Iterable[EventEnvelope]) -> None:
    """Raise :class:`BundlePayloadUnsafeError` if any event payload is unsafe.

    Scans every payload (recursively into nested dicts / lists) for any key
    listed in :data:`EXPORT_FORBIDDEN_PAYLOAD_KEYS`. The engine never emits
    these on its own; this catches a future bug or an externally-tampered
    bundle that injects model provenance / ratings / secrets into the event
    log before signing.
    """
    for event in events:
        _walk(event.payload, f"events[seq={event.sequence}].payload")


def _walk(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            sub_path = f"{path}.{key}"
            if key in EXPORT_FORBIDDEN_PAYLOAD_KEYS:
                raise BundlePayloadUnsafeError(f"forbidden bundle key {key!r} at {sub_path}")
            _walk(sub_value, sub_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]")


# --------------------------------------------------------------------------- #
# Hash-chain verification
# --------------------------------------------------------------------------- #


def _event_body(event: EventEnvelope) -> dict[str, Any]:
    """Reconstruct the event-body dict that was hashed when the row was written.

    Must match the shape produced by :func:`padrino.runner.game_runner._emit`:
    ``event_type, sequence, phase, visibility, actor_player_id, payload``.
    ``event_hash``, ``prev_event_hash``, and ``created_at`` are excluded from
    the hash by :mod:`padrino.core.engine.hashing` and so are omitted here.
    """
    return {
        "event_type": event.event_type,
        "sequence": event.sequence,
        "phase": event.phase,
        "visibility": event.visibility,
        "actor_player_id": event.actor_player_id,
        "payload": event.payload,
    }


def verify_chain(events: Sequence[EventEnvelope]) -> str:
    """Replay the chain through a fresh :class:`EventLog` and return the tip hash.

    Raises :class:`ReplayHashMismatchError` if any event's recomputed hash
    disagrees with the stored ``event_hash``.
    """
    stored = [
        StoredEvent(
            sequence=e.sequence,
            prev_event_hash=e.prev_event_hash,
            event_hash=e.event_hash,
            body=_event_body(e),
        )
        for e in events
    ]
    log = replay_event_log(stored)
    return log.head_hash


# Backwards-compatible alias for internal callers that referenced the
# pre-publication name (kept private leading underscore as the old export).
_verify_chain = verify_chain


# --------------------------------------------------------------------------- #
# Building the bundle
# --------------------------------------------------------------------------- #


async def export_game(
    session: AsyncSession,
    game_id: uuid.UUID,
    *,
    signer: Ed25519Signer | None = None,
) -> GameBundle:
    """Build a :class:`GameBundle` for the completed ``game_id``.

    Raises:
        GameNotExportable: game missing or not in ``COMPLETED`` status.
        BundlePayloadUnsafeError: an event payload contains a forbidden key.
        ReplayHashMismatchError: the persisted hash chain does not verify.
    """
    game = await games_repo.get(session, game_id)
    if game is None:
        raise GameNotExportable(f"game {game_id} not found")
    if game.status != "COMPLETED":
        raise GameNotExportable(f"game {game_id} is not COMPLETED (status={game.status!r})")

    seats = await games_repo.list_seats(session, game_id)
    event_rows = await events_repo.list_events(session, game_id)
    if not event_rows:
        raise GameNotExportable(f"game {game_id} has no events to export")

    events = [_envelope_from_row(row) for row in event_rows]
    assert_bundle_payload_safe(events)
    tip_hash = verify_chain(events)

    league_id: str | None = None
    if game.gauntlet_id is not None:
        gauntlet = await gauntlets_repo.get(session, game.gauntlet_id)
        if gauntlet is not None:
            league_id = str(gauntlet.league_id)

    agent_builds: list[AgentBuildInfo] = []
    for seat in seats:
        ab = await agent_builds_repo.get(session, seat.agent_build_id)
        if ab is None:
            raise GameNotExportable(
                f"seat {seat.public_player_id!r} references missing agent_build "
                f"{seat.agent_build_id}"
            )
        mc = await model_configs_repo.get(session, ab.model_config_id)
        pv = await prompt_versions_repo.get(session, ab.prompt_version_id)
        if mc is None or pv is None:
            raise GameNotExportable(
                f"seat {seat.public_player_id!r} agent_build {ab.id} has "
                f"missing model_config or prompt_version row"
            )
        provider = await providers_repo.get(session, mc.provider_id)
        provider_name = provider.name if provider is not None else "unknown"
        agent_builds.append(
            AgentBuildInfo(
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                display_name=ab.display_name,
                prompt_version=pv.version,
                model_provider=provider_name,
                model_name=mc.model_name,
                model_version=mc.model_version,
            )
        )

    bundle = GameBundle(
        ruleset_id=game.ruleset_id,
        league_id=league_id,
        gauntlet_id=str(game.gauntlet_id) if game.gauntlet_id is not None else None,
        game_id=str(game.id),
        seed=game.game_seed,
        terminal_result=dict(game.terminal_result) if game.terminal_result else None,
        tip_hash=tip_hash,
        agent_builds=agent_builds,
        game_seats=[
            GameSeatInfo(
                public_player_id=s.public_player_id,
                seat_index=s.seat_index,
                role=s.role,
                faction=s.faction,
                alive=s.alive,
                death_phase=s.death_phase,
            )
            for s in seats
        ],
        events=events,
    )

    if signer is not None:
        bundle = _attach_signature(bundle, signer)

    return bundle


def _envelope_from_row(row: GameEvent) -> EventEnvelope:
    return EventEnvelope(
        sequence=row.sequence,
        event_type=row.event_type,
        phase=row.phase,
        visibility=row.visibility,
        actor_player_id=row.actor_player_id,
        payload=dict(row.payload),
        prev_event_hash=row.prev_event_hash,
        event_hash=row.event_hash,
    )


def _attach_signature(bundle: GameBundle, signer: Ed25519Signer) -> GameBundle:
    fingerprinted = bundle.model_copy(update={"signer_fingerprint": signer.fingerprint})
    sig = signer.sign(canonical_bundle_bytes(fingerprinted))
    return fingerprinted.model_copy(update={"sig": sig})


__all__ = [
    "EXPORT_FORBIDDEN_PAYLOAD_KEYS",
    "SCHEMA_VERSION",
    "AgentBuildInfo",
    "BundlePayloadUnsafeError",
    "Ed25519Signer",
    "EventEnvelope",
    "ExportError",
    "GameBundle",
    "GameNotExportable",
    "GameSeatInfo",
    "ReplayHashMismatchError",
    "assert_bundle_payload_safe",
    "canonical_bundle_bytes",
    "export_game",
    "verify_bundle_signature",
    "verify_chain",
]
