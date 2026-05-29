"""Public game-bundle ingestion endpoint (US-062).

``POST /ingest/game`` accepts a :class:`padrino.export.bundle.GameBundle`
produced by US-061's export pipeline, verifies the hash chain (and the
Ed25519 signature when the submitter has a registered ``submission_public_key``),
and persists a row into ``ingested_games``.

Submission requires the ``submitter`` scope (admin keys also pass). Bundles
that are signed and whose signature verifies against a registered key are
stored with ``verification_status='verified'``; otherwise the row is stored
``unverified`` (and an admin can later require signed-only on read).

Idempotency: re-submission of the same ``bundle.game_id`` returns 200 with
``{already_ingested: true}`` and does not write a duplicate row. A tampered
or non-replayable bundle returns 422 ``hash_chain_mismatch`` and writes
nothing. Bundles larger than ``MAX_BUNDLE_BYTES`` are rejected with 413.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import (
    SCOPE_ADMIN,
    SCOPE_SUBMITTER,
    ApiKeyContext,
    require_scopes,
)
from padrino.api.deps import get_session
from padrino.core.rulesets import bench10_v1, mini7_v1
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import ingested_games as ingested_games_repo
from padrino.export.bundle import (
    BundlePayloadUnsafeError,
    EventEnvelope,
    GameBundle,
    ReplayHashMismatchError,
    assert_bundle_payload_safe,
    verify_bundle_signature,
    verify_chain,
)

MAX_BUNDLE_BYTES: int = 10 * 1024 * 1024

KNOWN_RULESET_IDS: Final[frozenset[str]] = frozenset({mini7_v1.RULESET_ID, bench10_v1.RULESET_ID})

router = APIRouter()
require_submit = require_scopes(SCOPE_ADMIN, SCOPE_SUBMITTER)


async def _read_bounded_body(request: Request) -> bytes:
    """Stream the request body, raising 413 as soon as the cap is exceeded.

    Honors a too-large ``Content-Length`` header up front so a lying client
    that claims a small body but streams a large one is cut off mid-stream
    rather than buffered in full.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_int = int(declared)
        except ValueError:
            declared_int = -1
        if declared_int > MAX_BUNDLE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="bundle_too_large",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BUNDLE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="bundle_too_large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _claimed_winner_matches_events(
    terminal_result: dict[str, object] | None,
    events: list[EventEnvelope],
) -> bool:
    """Return False iff the bundle claims a winner the event log doesn't record."""
    if terminal_result is None:
        return True
    claimed = terminal_result.get("winner")
    if claimed is None:
        return True
    for event in events:
        if event.event_type == "GameTerminated":
            return event.payload.get("winner") == claimed
    return False


@router.post("/ingest/game")
async def ingest_game(
    request: Request,
    ctx: ApiKeyContext = Depends(require_submit),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    raw_body = await _read_bounded_body(request)

    try:
        bundle = GameBundle.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_bundle", "errors": exc.errors()},
        ) from exc

    existing = await ingested_games_repo.get_by_game_id(session, bundle.game_id)
    if existing is not None:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "already_ingested": True,
                "game_id": existing.game_id,
                "verification_status": existing.verification_status,
            },
        )

    if bundle.ruleset_id not in KNOWN_RULESET_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "unknown_ruleset",
                "message": (
                    f"ruleset_id {bundle.ruleset_id!r} is not in the central node's "
                    f"whitelist {sorted(KNOWN_RULESET_IDS)}"
                ),
            },
        )

    try:
        assert_bundle_payload_safe(bundle.events)
    except BundlePayloadUnsafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "unsafe_payload", "message": str(exc)},
        ) from exc

    if not _claimed_winner_matches_events(bundle.terminal_result, bundle.events):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "inconsistent_terminal",
                "message": (
                    "bundle.terminal_result.winner does not match any GameTerminated "
                    "event in bundle.events"
                ),
            },
        )

    try:
        recomputed_tip = verify_chain(bundle.events)
    except ReplayHashMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "hash_chain_mismatch", "message": str(exc)},
        ) from exc
    if recomputed_tip != bundle.tip_hash:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "hash_chain_mismatch",
                "message": "recomputed tip does not match bundle.tip_hash",
            },
        )

    # Signature verification: requires both a signed bundle AND a registered
    # public key on the submitter's api_keys row. Anything else is stored as
    # ``unverified`` (admin contexts without a key id always store unverified).
    verification_status = ingested_games_repo.UNVERIFIED
    submitter_key_id = ctx.id
    if bundle.sig is not None:
        if submitter_key_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "signature_unverifiable",
                    "message": "signed bundle requires a submitter api_key with submission_public_key",
                },
            )
        submitter = await api_keys_repo.get(session, submitter_key_id)
        registered = submitter.submission_public_key if submitter is not None else None
        if registered is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "signature_unverifiable",
                    "message": "signed bundle requires submission_public_key registered on api_key",
                },
            )
        if not verify_bundle_signature(bundle, registered):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "signature_mismatch"},
            )
        verification_status = ingested_games_repo.VERIFIED

    obj = await ingested_games_repo.create(
        session,
        game_id=bundle.game_id,
        ruleset_id=bundle.ruleset_id,
        league_id=bundle.league_id,
        gauntlet_id=bundle.gauntlet_id,
        tip_hash=bundle.tip_hash,
        signer_fingerprint=bundle.signer_fingerprint,
        verification_status=verification_status,
        submitter_key_id=submitter_key_id,
        bundle=bundle.model_dump(mode="json"),
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "already_ingested": False,
            "id": str(obj.id),
            "game_id": obj.game_id,
            "verification_status": obj.verification_status,
            "tip_hash": obj.tip_hash,
        },
    )


__all__ = ["KNOWN_RULESET_IDS", "MAX_BUNDLE_BYTES", "router"]
