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

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import (
    SCOPE_ADMIN,
    SCOPE_SPECTATOR,
    ApiKeyContext,
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
from padrino.db.models import ApiKey, IngestedGame
from padrino.db.repositories import ingested_games as ingested_games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.ratings.model_rollup import (
    detail_for_model,
    rollup_by_model,
)
from padrino.ratings.model_rollup import (
    entry_to_response as model_entry_to_response,
)
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


async def require_public_read(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiKeyContext:
    """Allow anonymous reads when the public flag is on; otherwise spectator+."""
    cfg: Any = getattr(request.app.state, "auth_settings", None) or get_settings()
    if bool(cfg.padrino_public_leaderboard_anonymous):
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
    start = 0
    if query.cursor is not None:
        try:
            start = decode_index_cursor(query.cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc
    page = entries[start : start + query.limit]
    next_cursor = (
        encode_index_cursor(start + query.limit) if start + query.limit < len(entries) else None
    )
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
    start = 0
    if query.cursor is not None:
        try:
            start = decode_index_cursor(query.cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc
    page = events[start : start + query.limit]
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
    next_cursor = (
        encode_index_cursor(start + query.limit) if start + query.limit < len(events) else None
    )
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
    start = 0
    if query.cursor is not None:
        try:
            start = decode_index_cursor(query.cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc
    page = entries[start : start + query.limit]
    next_cursor = (
        encode_index_cursor(start + query.limit) if start + query.limit < len(entries) else None
    )
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


__all__ = [
    "PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS",
    "PublicChatEntry",
    "PublicEventEntry",
    "PublicEventsResponse",
    "PublicGameResponse",
    "PublicLeaderboardEntryResponse",
    "PublicLeaderboardResponse",
    "PublicModelBuildEntry",
    "PublicModelDetailResponse",
    "PublicModelEntryResponse",
    "PublicModelFactionAggregate",
    "PublicModelLeaderboardResponse",
    "PublicSubmitterEntry",
    "PublicSubmittersResponse",
    "PublicTranscriptResponse",
    "require_public_read",
    "router",
]
