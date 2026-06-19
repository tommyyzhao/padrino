"""Guest quickplay + human self-profile routes (US-128).

``POST /human/guest`` mints a guest *principal* and an opaque session token,
persists only the token's sha256 (constant-time compared on lookup), and sets an
http-only + ``SameSite=Lax`` cookie holding the plaintext token. It never touches
``api_keys`` and grants ZERO API scope — a guest cookie is a human identity only.
The endpoint is reachable even when ``create_app(auth_required=True)`` because it
carries no API-scope dependency (the human auth path is fully separate from the
API-key path, US-127).

``PATCH /human/me`` sets a per-session display name (validated, not globally
unique); ``GET /human/me`` returns the current principal. Both require a valid
human session via :func:`padrino.api.human_auth.require_human`.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import _get_auth_settings
from padrino.api.deps import get_session
from padrino.api.human_actions import submit_action
from padrino.api.human_auth import (
    HUMAN_SESSION_COOKIE,
    HumanPrincipalContext,
    generate_session_token,
    require_human,
)
from padrino.api.human_chat import submit_chat
from padrino.api.human_consent import (
    client_ip_hash,
    enforce_consent,
    has_current_consent,
    record_consent,
    required_consent_versions,
)
from padrino.api.human_observation import build_seat_observation_snapshot, stream_snapshot
from padrino.api.oauth import (
    OAuthError,
    build_authorization_request,
    exchange_code,
    resolve_oauth_config,
)
from padrino.core.engine.actions import Action
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.repositories import oauth_identities as oauth_repo

router = APIRouter()

OAUTH_STATE_COOKIE = "padrino_oauth_state"
OAUTH_VERIFIER_COOKIE = "padrino_oauth_verifier"
_OAUTH_FLOW_TTL_SECONDS = 600


class GuestSummary(BaseModel):
    """Public summary of a guest/account human principal (no PII beyond name)."""

    principal_id: uuid.UUID
    kind: str
    display_name: str | None


class DisplayNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("display_name")
    @classmethod
    def _strip_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name must not be blank")
        return stripped


class ConsentStatus(BaseModel):
    """Whether the current human has accepted the CURRENT legal documents."""

    consented: bool
    required_versions: dict[str, str]


@router.get("/human/consent", response_model=ConsentStatus)
async def get_consent_status(
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> ConsentStatus:
    """Report whether the principal holds a current consent for every document."""
    settings = _get_auth_settings(request)
    consented = await has_current_consent(
        session, subject_principal_id=ctx.principal_id, settings=settings
    )
    return ConsentStatus(
        consented=consented,
        required_versions=required_consent_versions(settings),
    )


@router.post(
    "/human/consent",
    response_model=ConsentStatus,
    status_code=status.HTTP_201_CREATED,
)
async def post_consent(
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> ConsentStatus:
    """Record the one-tap combined consent (TOS + Privacy + 16+ age gate)."""
    settings = _get_auth_settings(request)
    versions = await record_consent(
        session,
        subject_principal_id=ctx.principal_id,
        settings=settings,
        accepted_at=datetime.now(UTC),
        source_ip_hash=client_ip_hash(request),
    )
    return ConsentStatus(consented=True, required_versions=versions)


class ActionSubmission(BaseModel):
    """A structured action a human submits for their seat (US-134).

    Exactly mirrors :class:`padrino.core.engine.actions.Action` (``type`` +
    optional ``target``) plus an ``idempotency_key`` that dedupes retries. No
    chat field is accepted here — chat is a separate channel (US-135) and only
    the structured action drives state (chat firewall).
    """

    model_config = ConfigDict(extra="forbid")

    action: Action
    idempotency_key: str = Field(min_length=1, max_length=200)


class ActionResult(BaseModel):
    """The accepted (or idempotently replayed) action submission."""

    accepted: bool
    public_player_id: str
    phase: str
    action_type: str
    target: str | None
    idempotent_replay: bool


@router.post(
    "/human/games/{game_id}/actions",
    response_model=ActionResult,
)
async def post_action(
    game_id: uuid.UUID,
    body: ActionSubmission,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> ActionResult:
    """Submit a structured action for the caller's seat over the action channel.

    Gated by consent (US-130). The action is validated server-side against the
    seat's legal actions in the current phase and buffered; an idempotency key
    dedupes retries so a network retry never double-votes.
    """
    settings = _get_auth_settings(request)
    await enforce_consent(session, subject_principal_id=ctx.principal_id, settings=settings)
    accepted = await submit_action(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
        action=body.action,
        idempotency_key=body.idempotency_key,
        now=datetime.now(UTC),
    )
    return ActionResult(
        accepted=True,
        public_player_id=accepted.public_player_id,
        phase=accepted.phase,
        action_type=accepted.action_type,
        target=accepted.target,
        idempotent_replay=accepted.idempotent_replay,
    )


class ChatSubmission(BaseModel):
    """A public/private chat message a human submits for their seat (US-135).

    The chat firewall holds: this channel accepts ONLY chat (a ``channel`` +
    ``text`` + an ``idempotency_key`` that dedupes retries). A stray structured
    ``action`` field is a 422 (``extra='forbid'``) — only the separate action
    channel (US-134) drives state. ``max_length`` is the ruleset message ceiling;
    the service re-checks the per-channel limit.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["PUBLIC", "PRIVATE"] = "PUBLIC"
    text: str = Field(min_length=1, max_length=600)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ChatResult(BaseModel):
    """The accepted (or idempotently replayed) chat submission."""

    accepted: bool
    public_player_id: str
    phase: str
    channel: str
    status: str
    idempotent_replay: bool


@router.post(
    "/human/games/{game_id}/chat",
    response_model=ChatResult,
)
async def post_chat(
    game_id: uuid.UUID,
    body: ChatSubmission,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> ChatResult:
    """Submit a chat message into the buffered hold over the chat channel.

    Gated by consent (US-130). The message enters the buffer hold and is released
    only after the block-before-release moderation hook passes (US-140 lands the
    verdict; US-135 ships a stub-pass gate); on release the raw text is routed to
    the out-of-band sidecar (US-123), never inline in a hash-chained payload. An
    idempotency key dedupes retries so a network retry never double-posts.
    """
    settings = _get_auth_settings(request)
    await enforce_consent(session, subject_principal_id=ctx.principal_id, settings=settings)
    accepted = await submit_chat(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
        channel=body.channel,
        text=body.text,
        idempotency_key=body.idempotency_key,
        now=datetime.now(UTC),
    )
    return ChatResult(
        accepted=True,
        public_player_id=accepted.public_player_id,
        phase=accepted.phase,
        channel=accepted.channel,
        status=accepted.status,
        idempotent_replay=accepted.idempotent_replay,
    )


@router.get("/human/games/{game_id}/observation/stream")
async def get_seat_observation_stream(
    game_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream the caller's seat observation + the current phase-deadline frame.

    A seat-scoped live stream (US-136): the seat's own identity-mode-aware
    observation projection (its private events + legal actions) followed by the
    transport-only phase-deadline frame carrying the wall-clock deadline. The
    deadline frame is emitted over the wire ONLY and is never written to the
    hash-chained log (hard rule 4). In anonymous mode the stream carries no
    human-vs-AI / model identity markers. A request for a seat the caller does
    not occupy is rejected (403).
    """
    snapshot = await build_seat_observation_snapshot(
        session, game_id=game_id, principal_id=ctx.principal_id
    )
    return StreamingResponse(
        stream_snapshot(snapshot),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/human/guest",
    response_model=GuestSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_guest(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> GuestSummary:
    """Create a guest principal + session and set the human session cookie."""
    settings = _get_auth_settings(request)
    raw_token = generate_session_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=settings.padrino_human_session_ttl_hours)

    principal = await principals_repo.create_principal(
        session, kind=principals_repo.PRINCIPAL_KIND_GUEST
    )
    await principals_repo.create_session(
        session,
        principal_id=principal.id,
        raw_token=raw_token,
        kind=principals_repo.SESSION_KIND_GUEST,
        issued_at=now,
        expires_at=expires_at,
    )

    response.set_cookie(
        key=HUMAN_SESSION_COOKIE,
        value=raw_token,
        max_age=settings.padrino_human_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.padrino_human_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return GuestSummary(
        principal_id=principal.id,
        kind=principal.kind,
        display_name=principal.display_name,
    )


@router.get("/human/me", response_model=GuestSummary)
async def get_me(
    ctx: HumanPrincipalContext = Depends(require_human),
) -> GuestSummary:
    """Return the current human principal summary."""
    return GuestSummary(
        principal_id=ctx.principal_id,
        kind=ctx.kind,
        display_name=ctx.display_name,
    )


@router.patch("/human/me", response_model=GuestSummary)
async def patch_me(
    body: DisplayNameUpdate,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> GuestSummary:
    """Set the current principal's display name (validated, not unique)."""
    updated = await principals_repo.set_display_name(
        session,
        ctx.principal_id,
        display_name=body.display_name,
        now=datetime.now(UTC),
    )
    assert updated is not None  # require_human guarantees the principal exists
    return GuestSummary(
        principal_id=updated.id,
        kind=updated.kind,
        display_name=updated.display_name,
    )


def _set_oauth_flow_cookie(response: Response, key: str, value: str, *, secure: bool) -> None:
    response.set_cookie(
        key=key,
        value=value,
        max_age=_OAUTH_FLOW_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


@router.get("/human/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    request: Request,
) -> RedirectResponse:
    """Begin the OAuth code flow: redirect to the provider with state + PKCE."""
    settings = _get_auth_settings(request)
    config = resolve_oauth_config(settings, provider)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="oauth_not_configured",
        )
    auth_request = build_authorization_request(config)
    response = RedirectResponse(
        url=auth_request.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
    secure = settings.padrino_human_session_cookie_secure
    _set_oauth_flow_cookie(response, OAUTH_STATE_COOKIE, auth_request.state, secure=secure)
    _set_oauth_flow_cookie(
        response, OAUTH_VERIFIER_COOKIE, auth_request.code_verifier, secure=secure
    )
    return response


@router.get("/human/oauth/{provider}/callback", response_model=GuestSummary)
async def oauth_callback(
    provider: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
) -> GuestSummary:
    """Complete the code flow: validate CSRF state, exchange, issue an account."""
    settings = _get_auth_settings(request)
    config = resolve_oauth_config(settings, provider)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="oauth_not_configured",
        )

    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    code_verifier = request.cookies.get(OAUTH_VERIFIER_COOKIE)
    if code is None or state is None or expected_state is None or code_verifier is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_state_missing")
    if not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_state_mismatch")

    try:
        user_info = await exchange_code(config, code=code, code_verifier=code_verifier)
    except OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_exchange_failed"
        ) from exc

    now = datetime.now(UTC)
    guest_id = await _in_flight_guest_id(session, request)
    account = await oauth_repo.find_or_create_account(
        session,
        provider=config.provider,
        subject=user_info.subject,
        display_name=user_info.display_name,
        now=now,
        upgrade_guest_id=guest_id,
    )

    raw_token = generate_session_token()
    expires_at = now + timedelta(hours=settings.padrino_human_session_ttl_hours)
    await principals_repo.create_session(
        session,
        principal_id=account.id,
        raw_token=raw_token,
        kind=principals_repo.SESSION_KIND_ACCOUNT,
        issued_at=now,
        expires_at=expires_at,
    )

    response.set_cookie(
        key=HUMAN_SESSION_COOKIE,
        value=raw_token,
        max_age=settings.padrino_human_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.padrino_human_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    response.delete_cookie(OAUTH_VERIFIER_COOKIE, path="/")
    return GuestSummary(
        principal_id=account.id,
        kind=account.kind,
        display_name=account.display_name,
    )


async def _in_flight_guest_id(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """Resolve the active guest principal from the in-flight session cookie.

    Only an active (non-expired, non-revoked) GUEST session is upgraded; an
    existing account session is left untouched (no multi-account merge).
    """
    raw = request.cookies.get(HUMAN_SESSION_COOKIE)
    if raw is None:
        return None
    record = await principals_repo.get_session_by_token(session, raw.strip())
    if record is None or record.revoked_at is not None:
        return None
    principal = await principals_repo.get_principal(session, record.principal_id)
    if principal is None or principal.deleted_at is not None:
        return None
    if principal.kind != principals_repo.PRINCIPAL_KIND_GUEST:
        return None
    return principal.id


__all__ = [
    "OAUTH_STATE_COOKIE",
    "OAUTH_VERIFIER_COOKIE",
    "ActionResult",
    "ActionSubmission",
    "ChatResult",
    "ChatSubmission",
    "ConsentStatus",
    "DisplayNameUpdate",
    "GuestSummary",
    "router",
]
