"""Per-model rating rollup over the local ``ratings`` + ``game_seats`` tables.

US-067: roll openskill ratings up across every :class:`AgentBuild` that shares
the same ``(provider.name, model_config.model_name, model_config.model_version)``
identity, so an operator running several agent-build variants of the same
underlying LLM can compare models head-to-head without reasoning about each
variant first.

The rollup is intentionally read-only over ``ratings`` + ``game_seats``: it
never recomputes from raw events. Aggregation rule:

* games-weighted mean mu — ``sum(mu_i * n_i) / sum(n_i)``
* sigma propagation — ``sqrt(sum(sigma_i^2 * n_i^2)) / sum(n_i)``
* conservative score — ``mu - 3 * sigma`` post-aggregation

where ``n_i`` is the rating row's ``games`` counter. Both the GLOBAL and the
per-faction scopes are aggregated independently from the FACTION-scope values
persisted in ``ratings``. Per-faction games / wins / draws / losses come from
``game_seats`` joined to
``Game.terminal_result`` (filter on ``Game.status == 'COMPLETED'``).

Cache is process-local and keyed on the league/ruleset plus a tag derived from
rating updates and terminal submission-event diagnostics. A new rating update
or diagnostic event changes the tag so the next read naturally misses the cache
and recomputes.
``reset_cache()`` is exported so tests can force a recompute.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction, Role
from padrino.db.game_status import GAME_STATUS_COMPLETED
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    Gauntlet,
    ModelConfig,
    ModelProvider,
    Rating,
)
from padrino.diagnostics.submissions import (
    INVALID_EVENT_TYPE,
    SUBMISSION_EVENT_TYPES,
    TIMEOUT_EVENT_TYPE,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_FACTION,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)

RATING_MODEL: Final[str] = "openskill_plackett_luce_v1"
_COMPLETED_STATUS: Final[str] = GAME_STATUS_COMPLETED
_LEGACY_FACTION_RESPONSE_SCOPES: Final[dict[str, str]] = {
    "town": Faction.TOWN.value,
    "mafia": Faction.MAFIA.value,
}


@dataclass(frozen=True, slots=True)
class FactionAggregate:
    """Per-faction sub-aggregate inside a :class:`ModelLeaderboardEntry`."""

    mu: float
    sigma: float
    conservative_score: float
    games: int
    wins: int
    draws: int
    losses: int


@dataclass(frozen=True, slots=True)
class RoleAggregate:
    """Exact-role sample-count diagnostic inside a model leaderboard entry."""

    games: int
    wins: int
    draws: int
    losses: int
    win_rate: float


@dataclass(frozen=True, slots=True)
class ModelLeaderboardEntry:
    """One row in the per-model leaderboard.

    ``model_key`` is the canonical ``'<provider>/<model_name>[@<version>]'``
    identifier used both as the row key and as the path parameter for the
    detail endpoint.
    """

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
    timeout_rate: float
    invalid_action_rate: float
    factions: dict[str, FactionAggregate]
    town: FactionAggregate
    mafia: FactionAggregate
    role_breakdown: dict[str, RoleAggregate]
    agent_build_count: int
    agent_build_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ModelRollup:
    league_id: uuid.UUID
    ruleset_id: str
    rating_model: str
    cache_tag: str
    entries: tuple[ModelLeaderboardEntry, ...]


@dataclass(frozen=True, slots=True)
class ModelBuildInfo:
    """One agent-build row exposed in the detail endpoint."""

    agent_build_id: uuid.UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class ModelDetail:
    entry: ModelLeaderboardEntry
    builds: tuple[ModelBuildInfo, ...]
    recent_game_ids: tuple[uuid.UUID, ...]


def model_key_for(provider: str, model_name: str, model_version: str | None) -> str:
    """Canonical ``'<provider>/<model_name>[@<version>]'`` identifier."""
    base = f"{provider}/{model_name}"
    if model_version is None or model_version == "":
        return base
    return f"{base}@{model_version}"


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


def _display_name_for(model_name: str, model_version: str | None) -> str:
    if model_version is None or model_version == "":
        return model_name
    return f"{model_name} @{model_version}"


@dataclass(slots=True)
class _ModelBucket:
    """Mutable accumulator for one (provider, model_name, model_version) group."""

    provider: str
    model_name: str
    model_version: str | None
    agent_build_ids: set[uuid.UUID] = field(default_factory=set)
    # Per-scope (mu_i, sigma_i, n_i) samples keyed by scope_value.
    rating_samples: dict[str, list[tuple[float, float, int]]] = field(default_factory=dict)
    faction_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    games: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    role_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    submissions: int = 0
    timeouts: int = 0
    invalids: int = 0


@dataclass(slots=True)
class _SeatCounterBucket:
    """Seat-derived outcome counts for one agent build."""

    games: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    faction_counts: dict[str, dict[str, int]] = field(default_factory=dict)


def _empty_outcome_counts() -> dict[str, int]:
    return {"games": 0, "wins": 0, "draws": 0, "losses": 0}


def _record_outcome(
    counts: dict[str, int],
    *,
    faction: str,
    winner: str | None,
) -> None:
    counts["games"] += 1
    if winner == "DRAW":
        counts["draws"] += 1
    elif winner == faction:
        counts["wins"] += 1
    elif winner is not None:
        counts["losses"] += 1


def _aggregate_rating(samples: list[tuple[float, float, int]]) -> tuple[float, float]:
    """Apply the games-weighted mu / propagation-style sigma formulas.

    Builds with ``n_i == 0`` are skipped — their mu/sigma are still the
    INITIAL_MU / INITIAL_SIGMA defaults and contribute no information. If
    every sample has zero games (e.g. a build was seeded but never played),
    the returned aggregate is the initial pair so consumers don't divide by
    zero.
    """
    weighted = [(mu, sigma, n) for mu, sigma, n in samples if n > 0]
    if not weighted:
        return INITIAL_MU, INITIAL_SIGMA
    total_n = sum(n for _mu, _sigma, n in weighted)
    if total_n == 0:  # pragma: no cover — guarded by the filter above
        return INITIAL_MU, INITIAL_SIGMA
    mu_agg = sum(mu * n for mu, _sigma, n in weighted) / total_n
    sigma_squared_sum = sum((sigma * n) ** 2 for _mu, sigma, n in weighted)
    sigma_agg = math.sqrt(sigma_squared_sum) / total_n
    return mu_agg, sigma_agg


def _faction_aggregate(bucket: _ModelBucket, faction: str) -> FactionAggregate:
    samples = bucket.rating_samples.get(faction, [])
    mu, sigma = _aggregate_rating(samples)
    counts = bucket.faction_counts.get(faction, _empty_outcome_counts())
    return FactionAggregate(
        mu=mu,
        sigma=sigma,
        conservative_score=_conservative(mu, sigma),
        games=counts["games"],
        wins=counts["wins"],
        draws=counts["draws"],
        losses=counts["losses"],
    )


def _faction_aggregates(bucket: _ModelBucket) -> dict[str, FactionAggregate]:
    faction_values = sorted(
        scope_value for scope_value in bucket.rating_samples if scope_value != SCOPE_VALUE_GLOBAL
    )
    return {faction: _faction_aggregate(bucket, faction) for faction in faction_values}


def _legacy_faction_response_fields(
    bucket: _ModelBucket,
    factions: Mapping[str, FactionAggregate],
) -> dict[str, FactionAggregate]:
    return {
        field: factions.get(scope_value, _faction_aggregate(bucket, scope_value))
        for field, scope_value in _LEGACY_FACTION_RESPONSE_SCOPES.items()
    }


def _role_aggregates(bucket: _ModelBucket) -> dict[str, RoleAggregate]:
    out: dict[str, RoleAggregate] = {}
    for role, counts in sorted(bucket.role_counts.items()):
        games = counts["games"]
        wins = counts["wins"]
        out[role] = RoleAggregate(
            games=games,
            wins=wins,
            draws=counts["draws"],
            losses=counts["losses"],
            win_rate=(wins / games) if games else 0.0,
        )
    return out


def _bucket_to_entry(bucket: _ModelBucket) -> ModelLeaderboardEntry:
    mu, sigma = _aggregate_rating(bucket.rating_samples.get(SCOPE_VALUE_GLOBAL, []))
    factions = _faction_aggregates(bucket)
    legacy_factions = _legacy_faction_response_fields(bucket, factions)
    return ModelLeaderboardEntry(
        model_key=model_key_for(bucket.provider, bucket.model_name, bucket.model_version),
        display_name=_display_name_for(bucket.model_name, bucket.model_version),
        model_provider=bucket.provider,
        model_name=bucket.model_name,
        model_version=bucket.model_version,
        mu=mu,
        sigma=sigma,
        conservative_score=_conservative(mu, sigma),
        games=bucket.games,
        wins=bucket.wins,
        draws=bucket.draws,
        losses=bucket.losses,
        timeout_rate=(bucket.timeouts / bucket.submissions) if bucket.submissions else 0.0,
        invalid_action_rate=(bucket.invalids / bucket.submissions) if bucket.submissions else 0.0,
        factions=factions,
        town=legacy_factions["town"],
        mafia=legacy_factions["mafia"],
        role_breakdown=_role_aggregates(bucket),
        agent_build_count=len(bucket.agent_build_ids),
        agent_build_ids=tuple(sorted(bucket.agent_build_ids, key=str)),
    )


_CACHE: dict[tuple[uuid.UUID, str, str], ModelRollup] = {}


def reset_cache() -> None:
    """Drop every cached rollup. Tests use this to force recomputation."""
    _CACHE.clear()


async def _cache_tag(
    session: AsyncSession,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> str:
    stmt = select(func.max(Rating.updated_at)).where(Rating.league_id == league_id)
    max_dt: datetime | None = (await session.execute(stmt)).scalar_one_or_none()
    events_stmt = (
        select(func.max(GameEvent.created_at), func.count(GameEvent.id))
        .select_from(GameEvent)
        .join(Game, Game.id == GameEvent.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(
            Gauntlet.league_id == league_id,
            Game.ruleset_id == ruleset_id,
            Game.status == _COMPLETED_STATUS,
            GameEvent.event_type.in_(SUBMISSION_EVENT_TYPES),
        )
    )
    max_event_dt, event_count = (await session.execute(events_stmt)).one()
    rating_part = max_dt.isoformat() if max_dt is not None else "empty"
    event_part = max_event_dt.isoformat() if max_event_dt is not None else "empty"
    return f"ratings:{rating_part};events:{event_part}:{int(event_count)}"


async def _build_identity_map(
    session: AsyncSession,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[str, str, str | None, str]]:
    """Return ``{agent_build_id: (provider, model_name, model_version, display_name)}``."""
    ids = list(agent_build_ids)
    if not ids:
        return {}
    stmt = (
        select(
            AgentBuild.id,
            AgentBuild.display_name,
            ModelProvider.name,
            ModelConfig.model_name,
            ModelConfig.model_version,
        )
        .join(ModelConfig, ModelConfig.id == AgentBuild.model_config_id)
        .join(ModelProvider, ModelProvider.id == ModelConfig.provider_id)
        .where(AgentBuild.id.in_(ids))
    )
    out: dict[uuid.UUID, tuple[str, str, str | None, str]] = {}
    for ab_id, display, provider, model_name, model_version in (await session.execute(stmt)).all():
        out[ab_id] = (str(provider), str(model_name), model_version, str(display))
    return out


async def _ratings_in_league(
    session: AsyncSession,
    league_id: uuid.UUID,
) -> list[Rating]:
    stmt = select(Rating).where(Rating.league_id == league_id)
    return list((await session.execute(stmt)).scalars().all())


async def _seat_counters_per_build(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> dict[uuid.UUID, _SeatCounterBucket]:
    """Aggregate per-(agent_build, faction) game counts for terminal games.

    Filters games on ``Game.status == 'COMPLETED'`` and
    ``Gauntlet.league_id == league_id`` (gauntlet is required so cross-league
    games can't leak into one league's rollup). Ruleset is enforced on the
    game row too, matching the route's filter.
    """
    stmt = (
        select(
            GameSeat.agent_build_id,
            GameSeat.faction,
            Game.terminal_result,
        )
        .join(Game, Game.id == GameSeat.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(
            Gauntlet.league_id == league_id,
            Game.ruleset_id == ruleset_id,
            Game.status == _COMPLETED_STATUS,
            GameSeat.agent_build_id.is_not(None),
        )
    )
    out: dict[uuid.UUID, _SeatCounterBucket] = defaultdict(_SeatCounterBucket)
    for ab_id, faction, terminal in (await session.execute(stmt)).all():
        if ab_id is None:
            continue
        faction_value = str(faction)
        bucket = out[ab_id]
        winner = terminal.get("winner") if isinstance(terminal, dict) else None
        total_counts = {
            "games": bucket.games,
            "wins": bucket.wins,
            "draws": bucket.draws,
            "losses": bucket.losses,
        }
        _record_outcome(total_counts, faction=faction_value, winner=winner)
        bucket.games = total_counts["games"]
        bucket.wins = total_counts["wins"]
        bucket.draws = total_counts["draws"]
        bucket.losses = total_counts["losses"]
        faction_counts = bucket.faction_counts.setdefault(faction_value, _empty_outcome_counts())
        _record_outcome(faction_counts, faction=faction_value, winner=winner)
    return dict(out)


async def _role_counters_per_build(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> dict[uuid.UUID, dict[str, dict[str, int]]]:
    """Aggregate exact-role sample counts for terminal games in one league."""
    stmt = (
        select(
            GameSeat.agent_build_id,
            GameSeat.faction,
            GameSeat.role,
            Game.terminal_result,
        )
        .join(Game, Game.id == GameSeat.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(
            Gauntlet.league_id == league_id,
            Game.ruleset_id == ruleset_id,
            Game.status == _COMPLETED_STATUS,
        )
    )
    out: dict[uuid.UUID, dict[str, dict[str, int]]] = {}
    for ab_id, faction, role_value, terminal in (await session.execute(stmt)).all():
        if ab_id is None:
            continue
        try:
            role = Role(role_value)
        except ValueError:
            continue
        ab_bucket = out.setdefault(ab_id, {})
        role_bucket = ab_bucket.setdefault(
            role.value, {"games": 0, "wins": 0, "draws": 0, "losses": 0}
        )
        role_bucket["games"] += 1
        winner = terminal.get("winner") if isinstance(terminal, dict) else None
        if winner == "DRAW":
            role_bucket["draws"] += 1
        elif winner == faction:
            role_bucket["wins"] += 1
        elif winner is not None:
            role_bucket["losses"] += 1
    return out


async def _submission_metrics_per_build(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> dict[uuid.UUID, dict[str, int]]:
    """Aggregate timeout and invalid-action attempts for terminal games."""

    stmt = (
        select(GameSeat.agent_build_id, GameEvent.event_type)
        .select_from(GameEvent)
        .join(Game, Game.id == GameEvent.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .join(
            GameSeat,
            (GameSeat.game_id == GameEvent.game_id)
            & (GameSeat.public_player_id == GameEvent.actor_player_id),
        )
        .where(
            Gauntlet.league_id == league_id,
            Game.ruleset_id == ruleset_id,
            Game.status == _COMPLETED_STATUS,
            GameEvent.event_type.in_(SUBMISSION_EVENT_TYPES),
            GameSeat.agent_build_id.is_not(None),
        )
    )
    out: dict[uuid.UUID, dict[str, int]] = defaultdict(
        lambda: {"submissions": 0, "timeouts": 0, "invalids": 0}
    )
    for ab_id, event_type in (await session.execute(stmt)).all():
        bucket = out[ab_id]
        bucket["submissions"] += 1
        if event_type == TIMEOUT_EVENT_TYPE:
            bucket["timeouts"] += 1
        elif event_type == INVALID_EVENT_TYPE:
            bucket["invalids"] += 1
    return dict(out)


async def rollup_by_model(
    session: AsyncSession,
    league_id: uuid.UUID,
    ruleset_id: str,
) -> ModelRollup:
    """Aggregate per-(provider, model_name, model_version) leaderboard rows.

    The result is cached on a league/ruleset tag derived from ratings plus
    terminal submission-event diagnostics. Pagination, scope checks and HTTP
    shape live in the route layer.
    """
    tag = await _cache_tag(session, league_id, ruleset_id)
    cache_key = (league_id, ruleset_id, tag)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    ratings = await _ratings_in_league(session, league_id)
    ab_ids = {r.agent_build_id for r in ratings}
    identities = await _build_identity_map(session, ab_ids)
    # Seat counts may reference agent_builds that haven't received a rating yet
    # (e.g. a draw before update — but our service always records GLOBAL on
    # every game). The two queries can drift; we union the ab_ids so a build
    # that appears in seats but not in ratings still produces a row.
    seat_counters = await _seat_counters_per_build(
        session, league_id=league_id, ruleset_id=ruleset_id
    )
    role_counters = await _role_counters_per_build(
        session, league_id=league_id, ruleset_id=ruleset_id
    )
    submission_metrics = await _submission_metrics_per_build(
        session, league_id=league_id, ruleset_id=ruleset_id
    )
    extra_ids = (
        set(seat_counters.keys()) | set(role_counters.keys()) | set(submission_metrics.keys())
    ) - ab_ids
    if extra_ids:
        identities.update(await _build_identity_map(session, extra_ids))

    buckets: dict[tuple[str, str, str | None], _ModelBucket] = {}

    def _bucket_for(ab_id: uuid.UUID) -> _ModelBucket | None:
        identity = identities.get(ab_id)
        if identity is None:
            return None
        provider, model_name, model_version, _display = identity
        key = (provider, model_name, model_version)
        b = buckets.get(key)
        if b is None:
            b = _ModelBucket(
                provider=provider,
                model_name=model_name,
                model_version=model_version,
            )
            buckets[key] = b
        b.agent_build_ids.add(ab_id)
        return b

    for rating in ratings:
        bucket = _bucket_for(rating.agent_build_id)
        if bucket is None:
            continue
        if rating.scope_type == SCOPE_GLOBAL and rating.scope_value == SCOPE_VALUE_GLOBAL:
            bucket.rating_samples.setdefault(SCOPE_VALUE_GLOBAL, []).append(
                (float(rating.mu), float(rating.sigma), int(rating.games))
            )
        elif rating.scope_type == SCOPE_FACTION:
            bucket.rating_samples.setdefault(rating.scope_value, []).append(
                (float(rating.mu), float(rating.sigma), int(rating.games))
            )

    for ab_id, seat_counts in seat_counters.items():
        bucket = _bucket_for(ab_id)
        if bucket is None:
            continue
        bucket.games += seat_counts.games
        bucket.wins += seat_counts.wins
        bucket.draws += seat_counts.draws
        bucket.losses += seat_counts.losses
        for faction, faction_counts in seat_counts.faction_counts.items():
            bucket_counts = bucket.faction_counts.setdefault(faction, _empty_outcome_counts())
            bucket_counts["games"] += faction_counts["games"]
            bucket_counts["wins"] += faction_counts["wins"]
            bucket_counts["draws"] += faction_counts["draws"]
            bucket_counts["losses"] += faction_counts["losses"]

    for ab_id, role_counts in role_counters.items():
        bucket = _bucket_for(ab_id)
        if bucket is None:
            continue
        for role, role_aggregate_counts in role_counts.items():
            bucket_counts = bucket.role_counts.setdefault(
                role, {"games": 0, "wins": 0, "draws": 0, "losses": 0}
            )
            bucket_counts["games"] += role_aggregate_counts["games"]
            bucket_counts["wins"] += role_aggregate_counts["wins"]
            bucket_counts["draws"] += role_aggregate_counts["draws"]
            bucket_counts["losses"] += role_aggregate_counts["losses"]

    for ab_id, metrics in submission_metrics.items():
        bucket = _bucket_for(ab_id)
        if bucket is None:
            continue
        bucket.submissions += metrics["submissions"]
        bucket.timeouts += metrics["timeouts"]
        bucket.invalids += metrics["invalids"]

    entries = sorted(
        (_bucket_to_entry(b) for b in buckets.values()),
        key=lambda e: (-e.conservative_score, e.model_key),
    )
    rollup = ModelRollup(
        league_id=league_id,
        ruleset_id=ruleset_id,
        rating_model=RATING_MODEL,
        cache_tag=tag,
        entries=tuple(entries),
    )
    _CACHE[cache_key] = rollup
    return rollup


async def detail_for_model(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    model_key: str,
    recent_game_limit: int = 25,
) -> ModelDetail | None:
    """Return one entry + its agent-builds and the last N completed game ids."""
    rollup = await rollup_by_model(session, league_id, ruleset_id)
    entry: ModelLeaderboardEntry | None = next(
        (e for e in rollup.entries if e.model_key == model_key), None
    )
    if entry is None:
        return None
    builds = await _builds_for_entry(session, entry.agent_build_ids)
    recent_games = await _recent_game_ids(
        session,
        league_id=league_id,
        ruleset_id=ruleset_id,
        agent_build_ids=entry.agent_build_ids,
        limit=recent_game_limit,
    )
    return ModelDetail(entry=entry, builds=builds, recent_game_ids=recent_games)


async def _builds_for_entry(
    session: AsyncSession,
    agent_build_ids: Iterable[uuid.UUID],
) -> tuple[ModelBuildInfo, ...]:
    ids = list(agent_build_ids)
    if not ids:
        return ()
    stmt = (
        select(AgentBuild.id, AgentBuild.display_name)
        .where(AgentBuild.id.in_(ids))
        .order_by(AgentBuild.display_name, AgentBuild.id)
    )
    rows = (await session.execute(stmt)).all()
    return tuple(ModelBuildInfo(agent_build_id=row[0], display_name=str(row[1])) for row in rows)


async def _recent_game_ids(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ruleset_id: str,
    agent_build_ids: Iterable[uuid.UUID],
    limit: int,
) -> tuple[uuid.UUID, ...]:
    ids = list(agent_build_ids)
    if not ids:
        return ()
    stmt = (
        select(Game.id, Game.created_at)
        .join(GameSeat, GameSeat.game_id == Game.id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(
            Gauntlet.league_id == league_id,
            Game.ruleset_id == ruleset_id,
            Game.status == _COMPLETED_STATUS,
            GameSeat.agent_build_id.in_(ids),
        )
        .order_by(desc(Game.created_at), desc(Game.id))
        .limit(limit * 7)  # each game can repeat up to 7 times via seat join
    )
    rows = (await session.execute(stmt)).all()
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for game_id, _created in rows:
        if game_id in seen:
            continue
        seen.add(game_id)
        out.append(game_id)
        if len(out) >= limit:
            break
    return tuple(out)


def entry_to_response(entry: ModelLeaderboardEntry) -> dict[str, object]:
    """Serialize a leaderboard entry into the FastAPI response shape."""

    def _faction_dict(f: FactionAggregate) -> Mapping[str, float | int]:
        return {
            "mu": f.mu,
            "sigma": f.sigma,
            "conservative_score": f.conservative_score,
            "games": f.games,
            "wins": f.wins,
            "draws": f.draws,
            "losses": f.losses,
        }

    def _role_dict(r: RoleAggregate) -> Mapping[str, float | int]:
        return {
            "games": r.games,
            "wins": r.wins,
            "draws": r.draws,
            "losses": r.losses,
            "win_rate": r.win_rate,
        }

    return {
        "model_key": entry.model_key,
        "display_name": entry.display_name,
        "model_provider": entry.model_provider,
        "model_name": entry.model_name,
        "model_version": entry.model_version,
        "mu": entry.mu,
        "sigma": entry.sigma,
        "conservative_score": entry.conservative_score,
        "games": entry.games,
        "wins": entry.wins,
        "draws": entry.draws,
        "losses": entry.losses,
        "timeout_rate": entry.timeout_rate,
        "invalid_action_rate": entry.invalid_action_rate,
        "factions": {
            faction: _faction_dict(aggregate) for faction, aggregate in entry.factions.items()
        },
        "town": _faction_dict(entry.town),
        "mafia": _faction_dict(entry.mafia),
        "role_breakdown": {
            role: _role_dict(aggregate) for role, aggregate in entry.role_breakdown.items()
        },
        "agent_build_count": entry.agent_build_count,
    }


__all__ = [
    "RATING_MODEL",
    "FactionAggregate",
    "ModelBuildInfo",
    "ModelDetail",
    "ModelLeaderboardEntry",
    "ModelRollup",
    "RoleAggregate",
    "detail_for_model",
    "entry_to_response",
    "model_key_for",
    "reset_cache",
    "rollup_by_model",
]
