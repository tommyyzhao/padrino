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
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import ingested_games as ingested_games_repo
from padrino.export.bundle import (
    BundlePayloadUnsafeError,
    GameBundle,
    ReplayHashMismatchError,
    assert_bundle_payload_safe,
    verify_bundle_signature,
    verify_chain,
)

MAX_BUNDLE_BYTES: int = 10 * 1024 * 1024

router = APIRouter()
require_submit = require_scopes(SCOPE_ADMIN, SCOPE_SUBMITTER)


@router.post("/ingest/game")
async def ingest_game(
    request: Request,
    ctx: ApiKeyContext = Depends(require_submit),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    raw_body = await request.body()
    if len(raw_body) > MAX_BUNDLE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="bundle_too_large",
        )

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

    try:
        assert_bundle_payload_safe(bundle.events)
    except BundlePayloadUnsafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "unsafe_payload", "message": str(exc)},
        ) from exc

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


__all__ = ["MAX_BUNDLE_BYTES", "router"]
