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

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import _get_auth_settings, _get_rate_limiter
from padrino.api.deps import get_session, get_session_factory
from padrino.api.human_actions import submit_action
from padrino.api.human_auth import (
    HUMAN_SESSION_COOKIE,
    HumanPrincipalContext,
    generate_session_token,
    get_human_context,
    require_human,
)
from padrino.api.human_chat import submit_chat
from padrino.api.human_chat_moderation import (
    RealtimeModerationHook,
    build_message_guard_from_settings,
)
from padrino.api.human_consent import (
    CONSENT_REQUIRED_DETAIL,
    client_ip_hash,
    enforce_consent,
    has_current_consent,
    record_consent,
    required_consent_versions,
)
from padrino.api.human_observation import build_seat_observation_snapshot, stream_snapshot
from padrino.api.human_seat_auth import HUMAN_LANE_SEAT_KINDS
from padrino.api.human_turing import get_own_result, submit_guess
from padrino.api.lobby_launch import (
    AutoFillPoolError,
    LobbyNotLaunchableError,
    launch_lobby,
)
from padrino.api.oauth import (
    OAuthError,
    build_authorization_request,
    exchange_code,
    oauth_session_binding,
    resolve_oauth_config,
    state_flow_token,
    validate_authorization_state,
)
from padrino.api.pagination import (
    InvalidCursorError,
    decode_index_cursor,
    encode_index_cursor,
    invalid_cursor_error,
)
from padrino.api.reveal import build_participant_reveal, winner_from_terminal_result
from padrino.core.engine.actions import Action
from padrino.core.enums import IdentityMode, LobbySeatKind, LobbyStatus
from padrino.core.reveal import EndgameReveal
from padrino.core.rulesets import get_ruleset
from padrino.db.game_status import GAME_STATUS_COMPLETED
from padrino.db.models import Game, GameSeat, HumanPlayerStats, HumanTuringGuess, Principal
from padrino.db.repositories import human_principals as principals_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import lobbies as lobbies_repo
from padrino.db.repositories import oauth_consumed_flows as oauth_flows_repo
from padrino.db.repositories import oauth_identities as oauth_repo
from padrino.economics.human_cost_governance import (
    ACTION_CREATE,
    ACTION_LAUNCH,
    HumanAdmitDecision,
    admit_human,
    bind_admission_slots,
    release_admission_for_lobby,
    release_inference_reservations_for_lobby,
    rollback_admission_decision,
)
from padrino.settings import Settings

router = APIRouter()

OAUTH_STATE_COOKIE = "padrino_oauth_state"
OAUTH_VERIFIER_COOKIE = "padrino_oauth_verifier"
_OAUTH_FLOW_TTL_SECONDS = 600
_OAUTH_FLOW_PRUNE_SAMPLE_MODULUS = 16
_SOLO_MATCH_RULESET_ID = "mini7_v1"
_ADMISSION_DENIED_STATUS = status.HTTP_429_TOO_MANY_REQUESTS


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


class MatchResponse(BaseModel):
    """Result of a solo instant match handoff."""

    game_id: uuid.UUID


class HumanRoleWinRate(BaseModel):
    """One client-shaped role win-rate item."""

    role: str
    wins: int
    games: int
    rate: float


class HumanVotingAccuracy(BaseModel):
    """Client-shaped voting accuracy counts and derived rate."""

    total_votes: int
    accurate_votes: int
    rate: float


class HumanStatsResponse(BaseModel):
    """Per-human casual stats projected from the materialized stats row."""

    ruleset_id: str
    principal_id: uuid.UUID
    games: int
    wins: int
    draws: int
    losses: int
    role_win_rates: list[HumanRoleWinRate]
    survival_rate: float
    voting_accuracy: HumanVotingAccuracy
    detection_accuracy: str


class HumanGameSpotTheAi(BaseModel):
    """The caller's own postgame spot-the-AI result, when already submitted."""

    total: int
    correct: int
    accuracy: str


class HumanGameHistoryEntry(BaseModel):
    """One completed human-lane game in the caller's private history."""

    game_id: uuid.UUID
    ruleset_id: str
    ended_at: datetime
    result: Literal["WIN", "LOSS", "DRAW", "UNKNOWN"]
    winner: str | None
    role: str
    spot_the_ai: HumanGameSpotTheAi | None
    reveal_path: str


class HumanGameHistoryResponse(BaseModel):
    """Bounded page of the caller's own completed human-lane games."""

    items: list[HumanGameHistoryEntry]
    next_cursor: str | None = None
    total_estimate: int


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _personal_game_result(
    *,
    winner: str | None,
    faction: str,
) -> Literal["WIN", "LOSS", "DRAW", "UNKNOWN"]:
    if winner is None:
        return "UNKNOWN"
    if winner == "DRAW":
        return "DRAW"
    return "WIN" if winner == faction else "LOSS"


def _detection_accuracy_string(row: HumanPlayerStats | None) -> str:
    if row is None or row.detection_total == 0:
        return "0"
    return f"{row.detection_accurate}/{row.detection_total}"


def _role_win_rates(raw_json: str) -> list[HumanRoleWinRate]:
    raw_rates: list[dict[str, Any]] = json.loads(raw_json)
    return [
        HumanRoleWinRate(
            role=str(rate.get("role", rate["name"])),
            wins=int(rate["wins"]),
            games=int(rate["games"]),
            rate=_ratio(int(rate["wins"]), int(rate["games"])),
        )
        for rate in raw_rates
    ]


def _project_human_stats(
    *,
    ruleset_id: str,
    principal_id: uuid.UUID,
    row: HumanPlayerStats | None,
) -> HumanStatsResponse:
    if row is None:
        return HumanStatsResponse(
            ruleset_id=ruleset_id,
            principal_id=principal_id,
            games=0,
            wins=0,
            draws=0,
            losses=0,
            role_win_rates=[],
            survival_rate=0.0,
            voting_accuracy=HumanVotingAccuracy(total_votes=0, accurate_votes=0, rate=0.0),
            detection_accuracy="0",
        )
    return HumanStatsResponse(
        ruleset_id=row.ruleset_id,
        principal_id=row.principal_id,
        games=row.games,
        wins=row.wins,
        draws=row.draws,
        losses=row.losses,
        role_win_rates=_role_win_rates(row.role_win_rates_json),
        survival_rate=_ratio(row.survived_games, row.games),
        voting_accuracy=HumanVotingAccuracy(
            total_votes=row.voting_total_votes,
            accurate_votes=row.voting_accurate_votes,
            rate=_ratio(row.voting_accurate_votes, row.voting_total_votes),
        ),
        detection_accuracy=_detection_accuracy_string(row),
    )


@router.get("/human/stats", response_model=HumanStatsResponse)
async def get_human_stats(
    ruleset_id: str = Query(min_length=1),
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> HumanStatsResponse:
    """Return the caller's casual materialized play stats for one ruleset."""
    stmt = select(HumanPlayerStats).where(
        HumanPlayerStats.ruleset_id == ruleset_id,
        HumanPlayerStats.principal_id == ctx.principal_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _project_human_stats(
        ruleset_id=ruleset_id,
        principal_id=ctx.principal_id,
        row=row,
    )


@router.get("/human/games", response_model=HumanGameHistoryResponse)
async def list_human_games(
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = Query(default=None),
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> HumanGameHistoryResponse:
    """Return the caller's own completed casual human-lane games."""
    start = 0
    if cursor is not None:
        try:
            start = decode_index_cursor(cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc

    filters = (
        GameSeat.occupant_principal_id == ctx.principal_id,
        GameSeat.seat_kind.in_(HUMAN_LANE_SEAT_KINDS),
        Game.status == GAME_STATUS_COMPLETED,
        Game.completed_at.is_not(None),
    )
    total_stmt = select(func.count()).select_from(Game).join(GameSeat).where(*filters)
    total = int((await session.execute(total_stmt)).scalar_one())

    stmt = (
        select(Game, GameSeat, HumanTuringGuess)
        .join(GameSeat, GameSeat.game_id == Game.id)
        .outerjoin(
            HumanTuringGuess,
            (HumanTuringGuess.game_id == Game.id)
            & (HumanTuringGuess.guesser_public_id == GameSeat.public_player_id),
        )
        .where(*filters)
        .order_by(Game.completed_at.desc(), Game.id.desc())
        .offset(start)
        .limit(limit + 1)
    )
    rows = list((await session.execute(stmt)).all())
    next_cursor = encode_index_cursor(start + limit) if len(rows) > limit else None
    items: list[HumanGameHistoryEntry] = []
    for game, seat, guess in rows[:limit]:
        assert game.completed_at is not None
        ended_at = game.completed_at
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=UTC)
        winner = winner_from_terminal_result(game.terminal_result)
        items.append(
            HumanGameHistoryEntry(
                game_id=game.id,
                ruleset_id=game.ruleset_id,
                ended_at=ended_at,
                result=_personal_game_result(winner=winner, faction=seat.faction),
                winner=winner,
                role=seat.role,
                spot_the_ai=None
                if guess is None
                else HumanGameSpotTheAi(
                    total=guess.total,
                    correct=guess.correct,
                    accuracy=guess.accuracy,
                ),
                reveal_path=f"/play/{game.id}/reveal",
            )
        )
    return HumanGameHistoryResponse(
        items=items,
        next_cursor=next_cursor,
        total_estimate=total,
    )


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


@router.post(
    "/human/match",
    response_model=MatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_match(
    request: Request,
    ctx: HumanPrincipalContext | None = Depends(get_human_context),
    session: AsyncSession = Depends(get_session),
) -> MatchResponse | JSONResponse:
    """Create a one-human casual lobby, auto-fill AI seats, and launch it.

    This is the solo "Play vs AI" handoff. It deliberately reuses the lobby
    launch path rather than hand-rolling game materialization, so auto-fill,
    role assignment, human-lane ownership, anonymity, and rating segregation
    stay identical to private friend lobbies.
    """
    settings = _get_auth_settings(request)
    if ctx is None:
        _principal, raw_token = await _create_guest_session(
            session,
            settings=settings,
            now=datetime.now(UTC),
        )
        await session.commit()
        return _consent_required_response(raw_token=raw_token, settings=settings)

    await enforce_consent(session, subject_principal_id=ctx.principal_id, settings=settings)

    create_admission = await _admit_or_429(
        session,
        settings=settings,
        principal_id=ctx.principal_id,
        action=ACTION_CREATE,
    )
    now = datetime.now(UTC)
    ruleset = get_ruleset(_SOLO_MATCH_RULESET_ID)
    league = await leagues_repo.get_or_create_humans_included(
        session,
        ruleset_id=_SOLO_MATCH_RULESET_ID,
        ranked=False,
    )
    lobby = await lobbies_repo.create_lobby(
        session,
        ruleset_id=_SOLO_MATCH_RULESET_ID,
        identity_mode=IdentityMode.ANONYMOUS.value,
        theme_pack_id=None,
        lobby_seed=secrets.token_hex(16),
        invite_token=secrets.token_urlsafe(24),
        integrity_acknowledged=False,
        host_principal_id=ctx.principal_id,
        league_id=league.id,
        now=now,
    )
    host_member = await lobbies_repo.add_member(
        session,
        lobby_id=lobby.id,
        principal_id=ctx.principal_id,
        is_host=True,
        now=now,
    )
    await bind_admission_slots(
        session,
        create_admission,
        lobby_id=lobby.id,
        lobby_member_id=host_member.id,
    )
    await lobbies_repo.add_seat(
        session,
        lobby_id=lobby.id,
        seat_index=0,
        seat_kind=LobbySeatKind.HUMAN,
        member_id=host_member.id,
    )
    for seat_index in range(1, ruleset.PLAYER_COUNT):
        await lobbies_repo.add_seat(
            session,
            lobby_id=lobby.id,
            seat_index=seat_index,
            seat_kind=LobbySeatKind.AI,
        )
    await lobbies_repo.set_lobby_status(
        session,
        lobby_id=lobby.id,
        status=LobbyStatus.LOCKED.value,
        now=now,
    )

    await release_admission_for_lobby(session, lobby_id=lobby.id, released_at=now)
    launch_admission = await _admit_or_429(
        session,
        settings=settings,
        principal_id=ctx.principal_id,
        action=ACTION_LAUNCH,
    )
    try:
        result = await launch_lobby(session, lobby_id=lobby.id)
    except (LobbyNotLaunchableError, AutoFillPoolError) as exc:
        await rollback_admission_decision(session, launch_admission)
        detail = "autofill_pool_exhausted" if isinstance(exc, AutoFillPoolError) else str(exc)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc

    if result.created:
        await bind_admission_slots(
            session,
            launch_admission,
            lobby_id=lobby.id,
            lobby_member_id=host_member.id,
        )
        await release_inference_reservations_for_lobby(
            session,
            lobby_id=lobby.id,
            released_at=now,
        )
    else:
        await rollback_admission_decision(session, launch_admission)

    await session.commit()
    return MatchResponse(game_id=result.game_id)


async def _admit_or_429(
    session: AsyncSession,
    *,
    settings: Settings,
    principal_id: uuid.UUID,
    action: str,
) -> HumanAdmitDecision:
    decision = await admit_human(
        session,
        settings,
        principal_id=principal_id,
        action=action,
    )
    if not decision.allowed:
        raise HTTPException(status_code=_ADMISSION_DENIED_STATUS, detail=decision.reason)
    return decision


def _consent_required_response(*, raw_token: str, settings: Settings) -> JSONResponse:
    response = JSONResponse(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        content={"detail": CONSENT_REQUIRED_DETAIL},
    )
    _set_human_session_cookie(response, raw_token=raw_token, settings=settings)
    return response


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
    rate_limit_store = _get_rate_limiter(request).store
    accepted = await submit_action(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
        action=body.action,
        idempotency_key=body.idempotency_key,
        now=datetime.now(UTC),
        rate_limit=rate_limit_store,
        per_principal_limit=settings.padrino_rate_limit_human_action_per_minute,
        per_game_phase_limit=settings.padrino_rate_limit_human_action_per_game_phase_per_minute,
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
    rate_limit_store = _get_rate_limiter(request).store
    accepted = await submit_chat(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
        channel=body.channel,
        text=body.text,
        idempotency_key=body.idempotency_key,
        now=datetime.now(UTC),
        moderation=RealtimeModerationHook(
            guard=build_message_guard_from_settings(settings),
            timeout_s=settings.padrino_human_chat_guard_timeout_seconds,
        ),
        rate_limit=rate_limit_store,
        per_principal_limit=settings.padrino_rate_limit_human_chat_per_minute,
        per_game_phase_limit=settings.padrino_rate_limit_human_chat_per_game_phase_per_minute,
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


@router.get(
    "/human/games/{game_id}/reveal",
    response_model=EndgameReveal,
)
async def get_human_game_reveal(
    game_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> EndgameReveal:
    """Return the canonical terminal reveal to a game participant.

    Private human games are often never public-broadcastable, so the public
    ``/reveal`` endpoint remains closed. This route is instead authenticated by
    the human-session cookie and requires the caller to occupy a seat in the
    terminal game. It reuses the single canonical reveal projection.
    """
    return await build_participant_reveal(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
    )


class TuringGuessSubmission(BaseModel):
    """A human's post-terminal spot-the-AI guess (US-144).

    ``guess`` maps every OTHER seat's ``public_player_id`` to ``"HUMAN"`` or
    ``"AI"``. This is a thin post-terminal step over the existing human channel,
    not a new FSM phase. No chat / action fields are accepted (``extra='forbid'``)
    - the imitation-game guess drives no game mechanics.
    """

    model_config = ConfigDict(extra="forbid")

    guess: dict[str, Literal["HUMAN", "AI"]] = Field(min_length=0)


class TuringGuessResult(BaseModel):
    """The caller's personal spot-the-AI detection accuracy (no leaderboard)."""

    guesser_public_id: str
    total: int
    correct: int
    accuracy: str
    idempotent_replay: bool


@router.post(
    "/human/games/{game_id}/turing-guess",
    response_model=TuringGuessResult,
)
async def post_turing_guess(
    game_id: uuid.UUID,
    body: TuringGuessSubmission,
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> TuringGuessResult:
    """Submit the post-terminal spot-the-AI guess and return personal accuracy.

    Gated by consent (US-130). The caller must occupy a seat in the game (403)
    and the game must be terminal (409). The guess is scored by the pure
    :func:`padrino.core.turing.scoring.score_guess` and persisted with the
    guesser's detection accuracy; a re-submission returns the stored result (a
    guesser guesses once). There is NO competitive leaderboard in v1.
    """
    settings = _get_auth_settings(request)
    await enforce_consent(session, subject_principal_id=ctx.principal_id, settings=settings)
    outcome = await submit_guess(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
        guess=dict(body.guess),
        now=datetime.now(UTC),
    )
    return TuringGuessResult(
        guesser_public_id=outcome.guesser_public_id,
        total=outcome.total,
        correct=outcome.correct,
        accuracy=outcome.accuracy,
        idempotent_replay=outcome.idempotent_replay,
    )


@router.get(
    "/human/games/{game_id}/turing-guess",
    response_model=TuringGuessResult,
)
async def get_turing_guess(
    game_id: uuid.UUID,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> TuringGuessResult:
    """Return the caller's own spot-the-AI accuracy, gated behind their guess.

    The personal accuracy result is disclosed ONLY after the viewer has submitted
    their guess: a caller who has not yet guessed gets a 404 (``guess_not_found``)
    so the reveal never hands out an accuracy stat for a guess that was not made.
    A caller who occupies no seat in the game is rejected (403).
    """
    outcome = await get_own_result(
        session,
        game_id=game_id,
        principal_id=ctx.principal_id,
    )
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="guess_not_found",
        )
    return TuringGuessResult(
        guesser_public_id=outcome.guesser_public_id,
        total=outcome.total,
        correct=outcome.correct,
        accuracy=outcome.accuracy,
        idempotent_replay=outcome.idempotent_replay,
    )


async def _create_guest_session(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime,
) -> tuple[Principal, str]:
    raw_token = generate_session_token()
    expires_at = now + timedelta(hours=settings.padrino_human_session_ttl_hours)
    principal = await principals_repo.create_principal(
        session,
        kind=principals_repo.PRINCIPAL_KIND_GUEST,
    )
    await principals_repo.create_session(
        session,
        principal_id=principal.id,
        raw_token=raw_token,
        kind=principals_repo.SESSION_KIND_GUEST,
        issued_at=now,
        expires_at=expires_at,
    )
    return principal, raw_token


def _set_human_session_cookie(
    response: Response,
    *,
    raw_token: str,
    settings: Settings,
) -> None:
    response.set_cookie(
        key=HUMAN_SESSION_COOKIE,
        value=raw_token,
        max_age=settings.padrino_human_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.padrino_human_session_cookie_secure,
        samesite="lax",
        path="/",
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
    now = datetime.now(UTC)
    principal, raw_token = await _create_guest_session(session, settings=settings, now=now)
    _set_human_session_cookie(response, raw_token=raw_token, settings=settings)
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
    auth_request = build_authorization_request(
        config,
        session_binding=oauth_session_binding(request.cookies.get(HUMAN_SESSION_COOKIE)),
    )
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
        nonce = validate_authorization_state(
            config,
            received_state=state,
            expected_state=expected_state,
            session_binding=oauth_session_binding(request.cookies.get(HUMAN_SESSION_COOKIE)),
        )
    except OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_state_mismatch"
        ) from exc

    # Single-use guard (US-202/US-208): durably claim this flow's unique token
    # BEFORE exchanging the code so a replayed (state cookie, code) pair fails
    # closed even if the provider exchange or later account/session work fails.
    now = datetime.now(UTC)
    try:
        flow_token = state_flow_token(config, state)
    except OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_state_mismatch"
        ) from exc
    claimed = await _try_consume_oauth_flow_durably(
        request,
        flow_token=flow_token,
        consumed_at=now,
    )
    if not claimed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_state_replayed")

    try:
        user_info = await exchange_code(
            config,
            code=code,
            code_verifier=code_verifier,
            nonce=nonce,
        )
    except OAuthError as exc:
        if exc.transient:
            await _release_oauth_flow_durably(request, flow_token=flow_token)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="oauth_exchange_failed"
        ) from exc

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


async def _try_consume_oauth_flow_durably(
    request: Request,
    *,
    flow_token: str,
    consumed_at: datetime,
) -> bool:
    """Claim an OAuth flow in its own transaction before provider exchange."""
    session_factory = get_session_factory(request)
    async with session_factory() as flow_session:
        if _oauth_flow_prune_due(flow_token):
            await oauth_flows_repo.prune_expired(
                flow_session,
                older_than=consumed_at - timedelta(seconds=_OAUTH_FLOW_TTL_SECONDS),
            )
        claimed = await oauth_flows_repo.try_consume_flow(
            flow_session,
            flow=flow_token,
            consumed_at=consumed_at,
        )
        await flow_session.commit()
    return claimed


async def _release_oauth_flow_durably(request: Request, *, flow_token: str) -> bool:
    """Release a transiently failed OAuth flow in its own transaction."""
    session_factory = get_session_factory(request)
    async with session_factory() as flow_session:
        released = await oauth_flows_repo.release_flow(flow_session, flow=flow_token)
        await flow_session.commit()
    return released


def _oauth_flow_prune_due(flow_token: str) -> bool:
    """Return True for a stable 1/N sample of OAuth callbacks.

    Expired consumed-flow rows are inert because every new authorization start
    mints a fresh random flow token. Deferring most sweeps is therefore
    functionally safe while avoiding a predicate DELETE on every callback.
    """
    digest = hashlib.sha256(flow_token.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], byteorder="big") % _OAUTH_FLOW_PRUNE_SAMPLE_MODULUS
    return bucket == 0


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
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
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
    "TuringGuessResult",
    "TuringGuessSubmission",
    "get_human_game_reveal",
    "router",
]
