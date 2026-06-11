"""Public read-only API for ingested results + federated leaderboard (US-063).

These routes aggregate every row in ``ingested_games`` into a shared,
identity-blind view of model performance. They are deliberately distinct
from the per-league surface under ``/leagues/{id}/*``:

* ``/public/leaderboard`` rolls up openskill across ingested bundles, keyed by
  the :class:`padrino.export.bundle.AgentBuildInfo` tuple. No submitter PII.
* ``/public/games/{id}`` returns the full bundle minus any submitter identity
  fields. ``/public/games/{id}/events`` paginates the event log.
  ``/public/games/{id}/transcript`` returns a transcript projection that
  drops any payload key listed in :data:`PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS`
  (role, faction, model identity, ratings, etc.) — these are reused from the
  ranked-observation guard so the contracts stay aligned.
* ``/public/submitters`` lists registered submitter labels + pubkey
  fingerprints + game count. Raw keys and contact info are NEVER exposed.

Auth is opt-in: when :attr:`Settings.padrino_public_leaderboard_anonymous`
is ``True`` the routes serve unauthenticated requests; otherwise the
spectator scope (or admin) is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.analytics.deterministic import compute_claim_analysis, compute_game_analytics
from padrino.api.auth import (
    SCOPE_ADMIN,
    SCOPE_SPECTATOR,
    ApiKeyContext,
    _get_rate_limiter,
    get_auth_context,
)
from padrino.api.deps import get_session
from padrino.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    InvalidCursorError,
    decode_index_cursor,
    encode_index_cursor,
    invalid_cursor_error,
)
from padrino.core.observation_privacy import FORBIDDEN_PAYLOAD_KEYS
from padrino.core.spectator_projection import project_events_for_spectator
from padrino.db.models import (
    AgentBuild,
    AnalyticsAggregate,
    ApiKey,
    Game,
    GameEvent,
    GameSeat,
    IngestedGame,
    League,
    Rating,
)
from padrino.db.repositories import ingested_games as ingested_games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.gauntlets.evaluation import evaluate_gauntlet, redact_for_public
from padrino.observability.metrics import broadcast_active_streams, record_broadcast_frame
from padrino.public.broadcast_index import BroadcastState, list_live
from padrino.public.broadcaster import CadenceConfig, default_cadence, plan_broadcast
from padrino.ratings.model_rollup import (
    detail_for_model,
    rollup_by_model,
)
from padrino.ratings.model_rollup import (
    entry_to_response as model_entry_to_response,
)
from padrino.ratings.openskill_service import SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL
from padrino.ratings.provisional_and_decay import apply_decay, days_idle, is_provisional, to_ordinal
from padrino.ratings.public_leaderboard import (
    compute_public_leaderboard,
    entry_to_response,
)
from padrino.settings import get_settings

router = APIRouter()

# Forbidden keys for the public transcript projection. Reuses the ranked
# observation guard's set so a leak that would be blocked in-game also gets
# stripped from the public artifact. The bundle's ``EXPORT_FORBIDDEN_PAYLOAD_KEYS``
# is intentionally NOT a superset: the public transcript additionally hides
# ``role`` / ``faction`` which appear in legitimate RolesAssigned payloads.
PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS

# Submitter-identity keys scrubbed from bundle responses.
_BUNDLE_PII_KEYS: frozenset[str] = frozenset({"submitter_key_id", "submitter_label"})

_PageItemT = TypeVar("_PageItemT")


def _paginate_index(
    items: Sequence[_PageItemT],
    limit: int,
    cursor: str | None,
) -> tuple[list[_PageItemT], str | None]:
    """Slice *items* per the opaque index cursor; 400 on a malformed cursor.

    Returns ``(page, next_cursor)`` — the shared decode → slice → encode
    pattern used by every index-cursor-paginated public route.
    """
    start = 0
    if cursor is not None:
        try:
            start = decode_index_cursor(cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc
    page = list(items[start : start + limit])
    next_cursor = encode_index_cursor(start + limit) if start + limit < len(items) else None
    return page, next_cursor


async def require_public_read(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiKeyContext:
    """Allow anonymous reads when the public flag is on; otherwise spectator+."""
    cfg: Any = getattr(request.app.state, "auth_settings", None) or get_settings()
    has_auth = (
        request.headers.get("Authorization") is not None
        or request.headers.get("X-Padrino-Admin-Token") is not None
    )

    if bool(cfg.padrino_public_leaderboard_anonymous) and not has_auth:
        limiter = _get_rate_limiter(request)
        xff = request.headers.get("X-Forwarded-For")
        ip = (
            xff.split(",")[0].strip()
            if xff
            else (request.client.host if request.client else "unknown")
        )
        ip_hash = hashlib.sha256(f"ip:{ip}".encode()).hexdigest()
        ceiling = cfg.padrino_rate_limit_anonymous_per_minute

        allowed, retry_after = await limiter.hit(ip_hash, limit_per_minute=ceiling)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate_limited",
                headers={"Retry-After": str(max(1, round(retry_after)))},
            )

        return ApiKeyContext(
            id=None,
            scopes=frozenset({SCOPE_SPECTATOR}),
            via_admin_token=False,
        )
    ctx = await get_auth_context(request, session)
    if not ctx.has_scope({SCOPE_ADMIN, SCOPE_SPECTATOR}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient_scope",
        )
    return ctx


class PublicLeaderboardQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = Field(min_length=1)
    gauntlet_id: str | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


class PublicLeaderboardEntryResponse(BaseModel):
    entity_id: str
    display_name: str
    model_provider: str
    model_name: str
    model_version: str | None
    prompt_version: str
    games: int
    wins: int
    draws: int
    losses: int
    mu: float
    sigma: float
    conservative_score: float


class PublicLeaderboardResponse(BaseModel):
    ruleset_id: str
    gauntlet_id: str | None
    rating_model: str
    cache_tag: str
    entries: list[PublicLeaderboardEntryResponse]
    next_cursor: str | None = None
    total_estimate: int


@router.get(
    "/public/leaderboard",
    response_model=PublicLeaderboardResponse,
)
async def public_leaderboard(
    query: Annotated[PublicLeaderboardQuery, Query()],
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicLeaderboardResponse:
    leaderboard = await compute_public_leaderboard(
        session,
        ruleset_id=query.ruleset_id,
        gauntlet_id=query.gauntlet_id,
    )
    entries = list(leaderboard.entries)
    page, next_cursor = _paginate_index(entries, query.limit, query.cursor)
    return PublicLeaderboardResponse(
        ruleset_id=leaderboard.ruleset_id,
        gauntlet_id=leaderboard.gauntlet_id,
        rating_model=leaderboard.rating_model,
        cache_tag=leaderboard.cache_tag,
        entries=[PublicLeaderboardEntryResponse(**entry_to_response(e)) for e in page],
        next_cursor=next_cursor,
        total_estimate=len(entries),
    )


async def _ingested_or_404(session: AsyncSession, game_id: str) -> IngestedGame:
    row = await ingested_games_repo.get_by_game_id(session, game_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ingested game {game_id} not found",
        )
    return row


def _scrub_bundle(bundle: Any) -> dict[str, Any]:
    """Strip submitter PII keys from a bundle dict.

    The :class:`GameBundle` schema doesn't actually carry those keys today;
    this is defense-in-depth against a future field addition or a tampered
    JSON column.
    """
    if not isinstance(bundle, dict):
        return {}
    return {k: v for k, v in bundle.items() if k not in _BUNDLE_PII_KEYS}


class PublicGameResponse(BaseModel):
    game_id: str
    ruleset_id: str
    league_id: str | None
    gauntlet_id: str | None
    tip_hash: str
    signer_fingerprint: str | None
    verification_status: str
    bundle: dict[str, Any]


@router.get(
    "/public/games/{game_id}",
    response_model=PublicGameResponse,
)
async def public_game(
    game_id: str,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicGameResponse:
    row = await _ingested_or_404(session, game_id)
    return PublicGameResponse(
        game_id=row.game_id,
        ruleset_id=row.ruleset_id,
        league_id=row.league_id,
        gauntlet_id=row.gauntlet_id,
        tip_hash=row.tip_hash,
        signer_fingerprint=row.signer_fingerprint,
        verification_status=row.verification_status,
        bundle=_scrub_bundle(row.bundle),
    )


class PublicEventEntry(BaseModel):
    sequence: int
    event_type: str
    phase: str
    visibility: str
    actor_player_id: str | None
    payload: dict[str, Any]
    prev_event_hash: str
    event_hash: str


class PublicEventsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


class PublicEventsResponse(BaseModel):
    game_id: str
    items: list[PublicEventEntry]
    next_cursor: str | None = None
    total_estimate: int


def _bundle_events(row: IngestedGame) -> list[dict[str, Any]]:
    bundle = row.bundle if isinstance(row.bundle, dict) else {}
    raw_events = bundle.get("events", [])
    return [event for event in raw_events if isinstance(event, dict)]


def _is_terminal(row: IngestedGame) -> bool:
    bundle = row.bundle if isinstance(row.bundle, dict) else {}
    terminal_result = bundle.get("terminal_result")
    return isinstance(terminal_result, dict) and bool(terminal_result.get("winner"))


@router.get(
    "/public/games/{game_id}/events",
    response_model=PublicEventsResponse,
)
async def public_game_events(
    game_id: str,
    query: Annotated[PublicEventsQuery, Query()],
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicEventsResponse:
    row = await _ingested_or_404(session, game_id)
    events = _bundle_events(row)
    if not _is_terminal(row):
        events = project_events_for_spectator(events)
    page, next_cursor = _paginate_index(events, query.limit, query.cursor)
    items = [
        PublicEventEntry(
            sequence=int(ev.get("sequence", 0)),
            event_type=str(ev.get("event_type", "")),
            phase=str(ev.get("phase", "")),
            visibility=str(ev.get("visibility", "")),
            actor_player_id=ev.get("actor_player_id"),
            payload=dict(ev.get("payload", {})),
            prev_event_hash=str(ev.get("prev_event_hash", "")),
            event_hash=str(ev.get("event_hash", "")),
        )
        for ev in page
    ]
    return PublicEventsResponse(
        game_id=game_id,
        items=items,
        next_cursor=next_cursor,
        total_estimate=len(events),
    )


def _payload_has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if key in PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS:
                return True
            if _payload_has_forbidden_key(sub_value):
                return True
    elif isinstance(value, list | tuple):
        for item in value:
            if _payload_has_forbidden_key(item):
                return True
    return False


class PublicChatEntry(BaseModel):
    sequence: int
    phase: str
    actor_player_id: str
    text: str


class PublicTranscriptResponse(BaseModel):
    game_id: str
    public_chat: list[PublicChatEntry]
    outcome: dict[str, Any] | None
    forbidden_payload_keys: list[str]


@router.get(
    "/public/games/{game_id}/transcript",
    response_model=PublicTranscriptResponse,
)
async def public_game_transcript(
    game_id: str,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicTranscriptResponse:
    row = await _ingested_or_404(session, game_id)
    public_chat: list[PublicChatEntry] = []
    for ev in _bundle_events(row):
        if ev.get("event_type") != "PublicMessageSubmitted":
            continue
        actor = ev.get("actor_player_id")
        payload = ev.get("payload", {})
        if not isinstance(actor, str) or not isinstance(payload, dict):
            continue
        if _payload_has_forbidden_key(payload):
            continue
        public_chat.append(
            PublicChatEntry(
                sequence=int(ev.get("sequence", 0)),
                phase=str(ev.get("phase", "")),
                actor_player_id=actor,
                text=str(payload.get("text", "")),
            )
        )

    bundle = row.bundle if isinstance(row.bundle, dict) else {}
    outcome_raw = bundle.get("terminal_result")
    outcome: dict[str, Any] | None = None
    if isinstance(outcome_raw, dict):
        outcome = {
            k: v for k, v in outcome_raw.items() if k not in PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS
        }

    return PublicTranscriptResponse(
        game_id=game_id,
        public_chat=public_chat,
        outcome=outcome,
        forbidden_payload_keys=sorted(PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS),
    )


# ---------------------------------------------------------------------------
# Analytics endpoints (US-104)
# ---------------------------------------------------------------------------


class PublicVotingAccuracyAnalytics(BaseModel):
    total_votes: int
    accurate_votes: int
    rate: float


class PublicSurvivalPointAnalytics(BaseModel):
    role: str
    day: int
    alive_count: int
    total_count: int
    fraction: float


class PublicRoleWinRateAnalytics(BaseModel):
    role: str
    wins: int
    games: int
    rate: float


class PublicClaimRecordAnalytics(BaseModel):
    player_id: str
    claimed_role: str
    sequence: int
    phase: str


class PublicCounterClaimGroupAnalytics(BaseModel):
    claimed_role: str
    claimants: list[str]


class PublicGameAnalyticsResponse(BaseModel):
    game_id: uuid.UUID
    ruleset_id: str
    winner: str | None
    voting_accuracy: PublicVotingAccuracyAnalytics
    survival_curve: list[PublicSurvivalPointAnalytics]
    role_win_rates: list[PublicRoleWinRateAnalytics] | None
    claims: list[PublicClaimRecordAnalytics]
    counter_claims: list[PublicCounterClaimGroupAnalytics]


class PublicModelAnalyticsResponse(BaseModel):
    agent_build_id: uuid.UUID
    ruleset_id: str
    version: str
    games_played: int
    role_win_rates: list[PublicRoleWinRateAnalytics]
    voting_accuracy: PublicVotingAccuracyAnalytics
    survival_curve: list[PublicSurvivalPointAnalytics]
    computed_at: datetime


@router.get(
    "/public/games/{game_id}/analytics",
    response_model=PublicGameAnalyticsResponse,
)
async def public_game_analytics(
    game_id: uuid.UUID,
    http_response: Response,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicGameAnalyticsResponse:
    """Return deterministic analytics for a broadcastable game.

    LIVE games return spoiler-safe analytics: ``winner`` and ``role_win_rates``
    are null so the outcome is not revealed before the broadcast completes.
    RECENT games include the full analytics and carry an immutable CDN cache
    header — once a game reaches RECENT the analytics never change.
    """
    game = await session.get(Game, game_id)
    if (
        game is None
        or not game.is_broadcastable
        or game.broadcast_state not in (BroadcastState.LIVE.value, BroadcastState.RECENT.value)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="game_not_found_or_not_broadcastable",
        )

    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    raw_events = list((await session.execute(stmt)).scalars())
    event_dicts: list[dict[str, Any]] = [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in raw_events
    ]

    analytics = compute_game_analytics(event_dicts)
    claim_analysis = compute_claim_analysis(event_dicts)
    is_live = game.broadcast_state == BroadcastState.LIVE.value

    if is_live:
        http_response.headers["Cache-Control"] = "no-store"
    else:
        http_response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    return PublicGameAnalyticsResponse(
        game_id=game_id,
        ruleset_id=game.ruleset_id,
        winner=None if is_live else analytics.winner,
        voting_accuracy=PublicVotingAccuracyAnalytics(
            total_votes=analytics.voting_accuracy.total_votes,
            accurate_votes=analytics.voting_accuracy.accurate_votes,
            rate=analytics.voting_accuracy.rate,
        ),
        survival_curve=[
            PublicSurvivalPointAnalytics(
                role=sp.role,
                day=sp.day,
                alive_count=sp.alive_count,
                total_count=sp.total_count,
                fraction=sp.fraction,
            )
            for sp in analytics.survival_curve
        ],
        role_win_rates=None
        if is_live
        else [
            PublicRoleWinRateAnalytics(
                role=rwr.role,
                wins=rwr.wins,
                games=rwr.games,
                rate=rwr.rate,
            )
            for rwr in analytics.role_win_rates
        ],
        claims=[
            PublicClaimRecordAnalytics(
                player_id=cr.player_id,
                claimed_role=cr.claimed_role,
                sequence=cr.sequence,
                phase=cr.phase,
            )
            for cr in claim_analysis.claims
        ],
        counter_claims=[
            PublicCounterClaimGroupAnalytics(
                claimed_role=ccg.claimed_role,
                claimants=list(ccg.claimants),
            )
            for ccg in claim_analysis.counter_claims
        ],
    )


class PublicSubmitterEntry(BaseModel):
    label: str
    key_prefix: str
    submission_public_key_fingerprint: str | None
    game_count: int


class PublicSubmittersResponse(BaseModel):
    items: list[PublicSubmitterEntry]
    total_estimate: int


def _fingerprint_pubkey(pubkey_b64: str | None) -> str | None:
    if pubkey_b64 is None:
        return None
    import base64
    import hashlib

    try:
        raw = base64.urlsafe_b64decode(pubkey_b64.encode("ascii"))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    return hashlib.sha256(raw).hexdigest()[:32]


@router.get(
    "/public/submitters",
    response_model=PublicSubmittersResponse,
)
async def public_submitters(
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicSubmittersResponse:
    counts = await ingested_games_repo.count_by_submitter(session)
    stmt = select(ApiKey).order_by(ApiKey.created_at, ApiKey.id)
    keys = list((await session.execute(stmt)).scalars())
    items: list[PublicSubmitterEntry] = []
    for key in keys:
        if key.id not in counts:
            continue
        items.append(
            PublicSubmitterEntry(
                label=key.label,
                key_prefix=key.key_prefix,
                submission_public_key_fingerprint=_fingerprint_pubkey(key.submission_public_key),
                game_count=counts.get(key.id, 0),
            )
        )
    return PublicSubmittersResponse(items=items, total_estimate=len(items))


class PublicModelFactionAggregate(BaseModel):
    mu: float
    sigma: float
    conservative_score: float
    games: int
    wins: int
    draws: int
    losses: int


class PublicModelEntryResponse(BaseModel):
    model_key: str
    display_name: str
    model_provider: str
    model_name: str
    model_version: str | None
    mu: float
    sigma: float
    conservative_score: float
    games: int
    wins: int
    draws: int
    losses: int
    town: PublicModelFactionAggregate
    mafia: PublicModelFactionAggregate
    agent_build_count: int


class PublicModelLeaderboardQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = Field(min_length=1)
    league_id: uuid.UUID
    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


class PublicModelLeaderboardResponse(BaseModel):
    league_id: uuid.UUID
    ruleset_id: str
    rating_model: str
    cache_tag: str
    entries: list[PublicModelEntryResponse]
    next_cursor: str | None = None
    total_estimate: int


class PublicModelBuildEntry(BaseModel):
    agent_build_id: uuid.UUID
    display_name: str


class PublicModelDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = Field(min_length=1)
    league_id: uuid.UUID


class PublicModelDetailResponse(BaseModel):
    league_id: uuid.UUID
    ruleset_id: str
    rating_model: str
    cache_tag: str
    entry: PublicModelEntryResponse
    builds: list[PublicModelBuildEntry]
    recent_game_ids: list[uuid.UUID]


async def _resolve_league_for_ruleset(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> None:
    """404 when the league is unknown; 422 when the ruleset doesn't match."""
    league = await leagues_repo.get(session, league_id)
    if league is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"league {league_id} not found",
        )
    if league.ruleset_id != ruleset_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ruleset_id_mismatch",
        )


@router.get(
    "/public/models/leaderboard",
    response_model=PublicModelLeaderboardResponse,
)
async def public_models_leaderboard(
    query: Annotated[PublicModelLeaderboardQuery, Query()],
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicModelLeaderboardResponse:
    await _resolve_league_for_ruleset(
        session, league_id=query.league_id, ruleset_id=query.ruleset_id
    )
    rollup = await rollup_by_model(session, query.league_id, query.ruleset_id)
    entries = list(rollup.entries)
    page, next_cursor = _paginate_index(entries, query.limit, query.cursor)
    return PublicModelLeaderboardResponse(
        league_id=rollup.league_id,
        ruleset_id=rollup.ruleset_id,
        rating_model=rollup.rating_model,
        cache_tag=rollup.cache_tag,
        entries=[PublicModelEntryResponse(**model_entry_to_response(e)) for e in page],  # type: ignore[arg-type]
        next_cursor=next_cursor,
        total_estimate=len(entries),
    )


@router.get(
    "/public/models/{agent_build_id}/analytics",
    response_model=PublicModelAnalyticsResponse,
)
async def public_model_analytics(
    agent_build_id: uuid.UUID,
    http_response: Response,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicModelAnalyticsResponse:
    """Return stored deterministic analytics aggregate for one agent build.

    Analytics are materialized by the offline aggregation job.  Returns 404
    if no aggregate has been computed yet for this agent.  Short CDN cache
    allows re-aggregation to propagate within minutes.
    """
    http_response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    stmt = (
        select(AnalyticsAggregate)
        .where(AnalyticsAggregate.agent_build_id == agent_build_id)
        .order_by(AnalyticsAggregate.computed_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"analytics not found for agent {agent_build_id}",
        )

    role_win_rates_data: list[dict[str, Any]] = json.loads(row.role_win_rates_json)
    survival_curve_data: list[dict[str, Any]] = json.loads(row.survival_curve_json)
    voting_rate = (
        row.voting_accurate_votes / row.voting_total_votes if row.voting_total_votes > 0 else 0.0
    )

    return PublicModelAnalyticsResponse(
        agent_build_id=agent_build_id,
        ruleset_id=row.ruleset_id,
        version=row.version,
        games_played=row.games_played,
        role_win_rates=[
            PublicRoleWinRateAnalytics(
                role=r["role"],
                wins=r["wins"],
                games=r["games"],
                rate=r["wins"] / r["games"] if r["games"] > 0 else 0.0,
            )
            for r in role_win_rates_data
        ],
        voting_accuracy=PublicVotingAccuracyAnalytics(
            total_votes=row.voting_total_votes,
            accurate_votes=row.voting_accurate_votes,
            rate=voting_rate,
        ),
        survival_curve=[
            PublicSurvivalPointAnalytics(
                role=sp["role"],
                day=sp["day"],
                alive_count=sp["alive_count"],
                total_count=sp["total_count"],
                fraction=sp["alive_count"] / sp["total_count"] if sp["total_count"] > 0 else 0.0,
            )
            for sp in survival_curve_data
        ],
        computed_at=row.computed_at,
    )


@router.get(
    "/public/models/{model_key:path}",
    response_model=PublicModelDetailResponse,
)
async def public_model_detail(
    query: Annotated[PublicModelDetailQuery, Query()],
    model_key: Annotated[str, Path(min_length=1)],
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicModelDetailResponse:
    await _resolve_league_for_ruleset(
        session, league_id=query.league_id, ruleset_id=query.ruleset_id
    )
    detail = await detail_for_model(
        session,
        league_id=query.league_id,
        ruleset_id=query.ruleset_id,
        model_key=model_key,
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"model {model_key} not found",
        )
    # Reuse the cache_tag from a fresh rollup call — rollup_by_model is cached
    # so this is a hashmap lookup, not a recomputation.
    rollup = await rollup_by_model(session, query.league_id, query.ruleset_id)
    return PublicModelDetailResponse(
        league_id=rollup.league_id,
        ruleset_id=rollup.ruleset_id,
        rating_model=rollup.rating_model,
        cache_tag=rollup.cache_tag,
        entry=PublicModelEntryResponse(**model_entry_to_response(detail.entry)),  # type: ignore[arg-type]
        builds=[
            PublicModelBuildEntry(
                agent_build_id=b.agent_build_id,
                display_name=b.display_name,
            )
            for b in detail.builds
        ],
        recent_game_ids=list(detail.recent_game_ids),
    )


@router.get("/public/gauntlets/{gauntlet_id}/report")
async def public_gauntlet_report(
    gauntlet_id: uuid.UUID,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a gauntlet evaluation report with model-identity columns scrubbed.

    Matches the privacy posture of the model-identity-free public leaderboard:
    agent performance keyed only by ``agent_build_id``, never by provider /
    model_name / display_name. The full identity-bearing report is available
    on the admin-scoped ``GET /gauntlets/{id}/report`` route.
    """
    report = await evaluate_gauntlet(gauntlet_id, session)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"gauntlet {gauntlet_id} not found",
        )
    return redact_for_public(report)


# ---------------------------------------------------------------------------
# SSE live broadcast endpoint (US-089, US-107)
# ---------------------------------------------------------------------------

#: Maximum speed multiplier for the ?speed= debug param.
_SSE_SPEED_MAX: float = 100.0

#: Active SSE connection count per IP hash. Managed synchronously within the
#: async event loop — no lock needed. Decremented in the generator's finally block.
_sse_active: dict[str, int] = {}


def _live_cadence() -> CadenceConfig:
    """Return the broadcast cadence config for the live SSE endpoint.

    Defined as a FastAPI dependency so tests can inject a zero-delay
    :class:`CadenceConfig` via ``app.dependency_overrides``.
    """
    return default_cadence()


@router.get("/public/games/{game_id}/live")
async def public_game_live_sse(
    game_id: uuid.UUID,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    speed: float = Query(default=1.0, ge=0.0001, le=_SSE_SPEED_MAX),
    _ctx: ApiKeyContext = Depends(require_public_read),
    cadence: CadenceConfig = Depends(_live_cadence),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a game's broadcast frames as Server-Sent Events.

    Each SSE message carries one ``public_event_v1`` frame; the SSE ``id:``
    field is set to the event's ``sequence`` number, enabling resumption via
    the standard ``Last-Event-ID`` reconnect header or the ``?after=``
    query parameter.

    Only LIVE and RECENT games are served; anything else returns 404.
    PRIVATE/SYSTEM events are silently dropped (identity-blind via US-086).
    The optional ``?speed=`` multiplier (capped at 100x) divides each
    frame's delay for debugging — the transport applies the delay before
    emitting each frame.
    """
    game = await session.get(Game, game_id)
    if (
        game is None
        or not game.is_broadcastable
        or game.broadcast_state
        not in (
            BroadcastState.LIVE.value,
            BroadcastState.RECENT.value,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="game_not_found_or_not_live",
        )

    # Per-IP SSE connection cap (US-107)
    xff = request.headers.get("X-Forwarded-For")
    client_ip = (
        xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    )
    ip_hash = hashlib.sha256(f"ip:{client_ip}".encode()).hexdigest()
    sse_cfg: Any = getattr(request.app.state, "auth_settings", None) or get_settings()
    sse_cap: int = sse_cfg.padrino_sse_max_connections_per_ip
    active_count = _sse_active.get(ip_hash, 0)
    if active_count >= sse_cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="sse_connection_limit_exceeded",
            headers={"Retry-After": "60"},
        )

    # Resolve cursor: Last-Event-ID header takes precedence over ?after=.
    # The server only emits integer sequence ids, so a non-integer header is
    # a client/proxy bug — reject it instead of silently replaying frame 0.
    cursor: int | None = after
    last_event_id_header = request.headers.get("Last-Event-ID")
    if last_event_id_header is not None:
        try:
            cursor = int(last_event_id_header)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_last_event_id_header",
            ) from exc

    stmt = select(GameEvent).where(GameEvent.game_id == game_id).order_by(GameEvent.sequence)
    if cursor is not None:
        # Resume filtering happens in SQL so a reconnect doesn't load and plan
        # the already-sent prefix. plan_broadcast is per-event (no inter-event
        # dependencies), so this is equivalent to post-filtering frames.
        stmt = stmt.where(GameEvent.sequence > cursor)
    result = await session.execute(stmt)
    raw_events = list(result.scalars())

    event_dicts: list[dict[str, Any]] = [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in raw_events
    ]

    frames = plan_broadcast(event_dicts, cadence)

    async def _generate() -> AsyncGenerator[str, None]:
        # The counter is incremented here rather than in the route body so it
        # is paired with the stream's actual lifetime: if the response body is
        # never iterated (client gone, error before send), neither the
        # increment nor the finally runs, and nothing leaks. The cap check
        # above reads the counter pre-increment, so a simultaneous burst from
        # one IP can briefly overshoot the cap — acceptable for an in-process
        # advisory limit.
        _sse_active[ip_hash] = _sse_active.get(ip_hash, 0) + 1
        broadcast_active_streams.inc()
        try:
            for frame in frames:
                seq = frame.event["sequence"]
                delay_s = (frame.delay_ms / speed) / 1000.0
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                data = json.dumps(frame.event, separators=(",", ":"))
                yield f"id: {seq}\ndata: {data}\n\n"
                record_broadcast_frame()
        finally:
            remaining = max(0, _sse_active.get(ip_hash, 0) - 1)
            if remaining:
                _sse_active[ip_hash] = remaining
            else:
                # Drop zeroed entries so the dict doesn't grow one key per
                # client IP ever seen.
                _sse_active.pop(ip_hash, None)
            broadcast_active_streams.dec()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Live/recent index endpoints (US-090)
# ---------------------------------------------------------------------------


class PublicLiveGameEntry(BaseModel):
    game_id: uuid.UUID
    ruleset_id: str
    current_phase: str | None
    players_alive: int


class PublicLiveIndexResponse(BaseModel):
    items: list[PublicLiveGameEntry]
    total: int


class PublicRecentGameEntry(BaseModel):
    game_id: uuid.UUID
    ruleset_id: str
    current_phase: str | None
    terminal_result: dict[str, Any] | None


class PublicRecentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


class PublicRecentIndexResponse(BaseModel):
    items: list[PublicRecentGameEntry]
    next_cursor: str | None = None
    total_estimate: int


@router.get(
    "/public/live",
    response_model=PublicLiveIndexResponse,
)
async def public_live_index(
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicLiveIndexResponse:
    """Return all currently LIVE broadcast games, spoiler-safe.

    ``terminal_result`` is absent from the response schema — the
    ``PublicLiveGameEntry`` model enforces spoiler safety at the transport layer.
    ``players_alive`` is the count of alive seats (0 if no seats are recorded).
    """
    live_entries = await list_live(session)
    game_ids = [e.game_id for e in live_entries]
    alive_counts: dict[uuid.UUID, int] = {}
    if game_ids:
        seats_stmt = select(GameSeat).where(
            GameSeat.game_id.in_(game_ids), GameSeat.alive.is_(True)
        )
        for seat in (await session.execute(seats_stmt)).scalars():
            alive_counts[seat.game_id] = alive_counts.get(seat.game_id, 0) + 1

    items = [
        PublicLiveGameEntry(
            game_id=e.game_id,
            ruleset_id=e.ruleset_id,
            current_phase=e.current_phase,
            players_alive=alive_counts.get(e.game_id, 0),
        )
        for e in live_entries
    ]
    return PublicLiveIndexResponse(items=items, total=len(items))


@router.get(
    "/public/recent",
    response_model=PublicRecentIndexResponse,
)
async def public_recent_index(
    query: Annotated[PublicRecentQuery, Query()],
    http_response: Response,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicRecentIndexResponse:
    """Return recently broadcast games (RECENT state) with outcome exposed.

    Ordered newest-first; paginated via the existing index-cursor mechanism.
    Short CDN cache so the list stays fresh as new games complete.
    """
    http_response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
    start = 0
    if query.cursor is not None:
        try:
            start = decode_index_cursor(query.cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc

    recent_filter = (
        Game.broadcast_state == BroadcastState.RECENT.value,
        Game.is_broadcastable.is_(True),
    )
    total = (
        await session.execute(select(func.count()).select_from(Game).where(*recent_filter))
    ).scalar_one()
    # Page in SQL: the RECENT set grows without bound, so loading it all to
    # slice one page in Python would scale with the whole game bank.
    stmt = (
        select(Game)
        .where(*recent_filter)
        .order_by(Game.created_at.desc())
        .offset(start)
        .limit(query.limit)
    )
    page = list((await session.execute(stmt)).scalars())
    next_cursor = encode_index_cursor(start + query.limit) if start + query.limit < total else None
    items = [
        PublicRecentGameEntry(
            game_id=g.id,
            ruleset_id=g.ruleset_id,
            current_phase=g.current_phase,
            terminal_result=g.terminal_result,
        )
        for g in page
    ]
    return PublicRecentIndexResponse(
        items=items,
        next_cursor=next_cursor,
        total_estimate=total,
    )


# ---------------------------------------------------------------------------
# Public ladder endpoint (US-100)
# ---------------------------------------------------------------------------


class PublicLadderQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = Field(min_length=1)
    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


class PublicLadderEntry(BaseModel):
    agent_build_id: uuid.UUID
    display_name: str
    version: str
    ordinal: int
    provisional: bool
    games: int
    last_game_at: datetime | None


class PublicLadderResponse(BaseModel):
    ruleset_id: str
    entries: list[PublicLadderEntry]
    next_cursor: str | None = None
    total_estimate: int


@router.get(
    "/public/ladder",
    response_model=PublicLadderResponse,
)
async def public_ladder(
    query: Annotated[PublicLadderQuery, Query()],
    request: Request,
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicLadderResponse:
    """Return per-ruleset agent ladder ranked by ELO-style ordinal.

    Ordinal is derived from OpenSkill (mu, sigma) with sigma inflation applied
    after the configured grace period of idleness. Provisional agents (< N games)
    are flagged but still included in the ranking.
    """
    cfg: Any = getattr(request.app.state, "auth_settings", None) or get_settings()
    now = datetime.now(UTC)

    stmt = (
        select(Rating, AgentBuild)
        .join(AgentBuild, Rating.agent_build_id == AgentBuild.id)
        .join(League, Rating.league_id == League.id)
        .where(
            League.ruleset_id == query.ruleset_id,
            Rating.scope_type == SCOPE_GLOBAL,
            Rating.scope_value == SCOPE_VALUE_GLOBAL,
        )
    )
    rows = list((await session.execute(stmt)).all())

    threshold: int = cfg.padrino_provisional_game_threshold
    grace_days: int = cfg.padrino_rating_decay_idle_days
    decay_rate: float = cfg.padrino_rating_decay_sigma_per_day

    entries: list[PublicLadderEntry] = []
    for rating, build in rows:
        idle = days_idle(rating.last_game_at, now=now)
        effective_idle = max(0, idle - grace_days)
        decayed_sigma = apply_decay(rating.sigma, effective_idle, decay_per_day=decay_rate)
        ordinal = to_ordinal(rating.mu, decayed_sigma)
        provisional = is_provisional(rating.games, threshold=threshold)
        entries.append(
            PublicLadderEntry(
                agent_build_id=build.id,
                display_name=build.display_name,
                version=build.version,
                ordinal=ordinal,
                provisional=provisional,
                games=rating.games,
                last_game_at=rating.last_game_at,
            )
        )

    entries.sort(key=lambda e: (-e.ordinal, str(e.agent_build_id)))

    total = len(entries)
    page, next_cursor = _paginate_index(entries, query.limit, query.cursor)

    return PublicLadderResponse(
        ruleset_id=query.ruleset_id,
        entries=page,
        next_cursor=next_cursor,
        total_estimate=total,
    )


__all__ = [
    "PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS",
    "PublicChatEntry",
    "PublicClaimRecordAnalytics",
    "PublicCounterClaimGroupAnalytics",
    "PublicEventEntry",
    "PublicEventsResponse",
    "PublicGameAnalyticsResponse",
    "PublicGameResponse",
    "PublicLadderEntry",
    "PublicLadderQuery",
    "PublicLadderResponse",
    "PublicLeaderboardEntryResponse",
    "PublicLeaderboardResponse",
    "PublicLiveGameEntry",
    "PublicLiveIndexResponse",
    "PublicModelAnalyticsResponse",
    "PublicModelBuildEntry",
    "PublicModelDetailResponse",
    "PublicModelEntryResponse",
    "PublicModelFactionAggregate",
    "PublicModelLeaderboardResponse",
    "PublicRecentGameEntry",
    "PublicRecentIndexResponse",
    "PublicRoleWinRateAnalytics",
    "PublicSubmitterEntry",
    "PublicSubmittersResponse",
    "PublicSurvivalPointAnalytics",
    "PublicTranscriptResponse",
    "PublicVotingAccuracyAnalytics",
    "_live_cadence",
    "_sse_active",
    "public_game_analytics",
    "public_game_live_sse",
    "public_ladder",
    "public_live_index",
    "public_model_analytics",
    "public_recent_index",
    "require_public_read",
    "router",
]
