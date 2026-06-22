"""Federated public-leaderboard aggregation and context cards.

Computes an openskill rollup across every row in ``ingested_games`` matching
``ruleset_id`` (and optionally ``gauntlet_id``), keyed by the
``(display_name, model_provider, model_name, model_version, prompt_version)``
tuple that the export bundle's :class:`AgentBuildInfo` already carries.

The leaderboard is recomputed on read, but cached behind
``max(ingested_games.created_at)`` — new ingestions invalidate the cache
naturally because the tag changes the moment a fresh row lands. The cache is
per process and intentionally simple: a single dict keyed by
``(ruleset_id, gauntlet_id, cache_tag)``.

US-186 adds per-context cards from the persisted rating tables. Those cards are
sectioned into canonical vs. experimental arrays and ranked only within their
exact context/ruleset/scope group, never across rating-context kinds.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal

from openskill.models import PlackettLuce
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LeagueKind, RatingContextKind
from padrino.db.models import (
    AgentBuild,
    IngestedGame,
    League,
    ModelConfig,
    ModelProvider,
    PlacementRating,
    PromptVersion,
    Rating,
    RatingContext,
    SoloRateRating,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)
from padrino.ratings.provisional_and_decay import DEFAULT_PROVISIONAL_GAMES
from padrino.ratings.solo_rate_service import (
    DEFAULT_SOLO_RATE_MIN_ATTEMPTS,
    beta_binomial_credible_interval,
)

RATING_MODEL: Final[str] = "openskill_plackett_luce_v1"

_TOWN: Final[Literal["TOWN"]] = "TOWN"
_MAFIA: Final[Literal["MAFIA"]] = "MAFIA"
_DRAW: Final[Literal["DRAW"]] = "DRAW"


@dataclass(frozen=True, slots=True)
class PublicEntity:
    """Stable identity for one row in the public leaderboard.

    The :attr:`entity_id` is a short sha256 fingerprint of the tuple — opaque
    to clients but reproducible across deployments given the same bundles.
    """

    entity_id: str
    display_name: str
    model_provider: str
    model_name: str
    model_version: str | None
    prompt_version: str


@dataclass(frozen=True, slots=True)
class PublicLeaderboardEntry:
    entity: PublicEntity
    games: int
    wins: int
    draws: int
    losses: int
    mu: float
    sigma: float
    conservative_score: float


@dataclass(frozen=True, slots=True)
class PublicRatingCard:
    """One per-context public score card.

    Cards are ranked only within their exact ``(context_kind, ruleset_id,
    scope_type, scope_value)`` group. Provisional cards are displayed but never
    assigned a rank.
    """

    card_id: str
    section: Literal["canonical", "experimental"]
    section_label: str
    context_kind: str
    context_label: str
    ruleset_id: str
    entity: PublicEntity
    scope_type: str
    scope_value: str
    metric: Literal["openskill_conservative", "solo_success_rate"]
    metric_label: str
    score: float
    rank: int | None
    provisional: bool
    provisional_reason: str | None
    sample_count: int
    games: int | None
    attempts: int | None
    successes: int | None
    mu: float | None
    sigma: float | None
    conservative_score: float | None
    mean_success_rate: float | None
    credible_interval_low: float | None
    credible_interval_high: float | None


@dataclass(frozen=True, slots=True)
class PublicLeaderboard:
    ruleset_id: str | None
    gauntlet_id: str | None
    rating_model: str
    cache_tag: str
    entries: tuple[PublicLeaderboardEntry, ...]
    canonical_cards: tuple[PublicRatingCard, ...]
    experimental_cards: tuple[PublicRatingCard, ...]


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


def _entity_id(
    *,
    display_name: str,
    model_provider: str,
    model_name: str,
    model_version: str | None,
    prompt_version: str,
) -> str:
    raw = "|".join(
        [
            display_name,
            model_provider,
            model_name,
            model_version or "",
            prompt_version,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _build_entity(ab: Mapping[str, Any]) -> PublicEntity:
    display_name = str(ab.get("display_name", ""))
    model_provider = str(ab.get("model_provider", ""))
    model_name = str(ab.get("model_name", ""))
    raw_version = ab.get("model_version")
    model_version = None if raw_version is None else str(raw_version)
    prompt_version = str(ab.get("prompt_version", ""))
    return PublicEntity(
        entity_id=_entity_id(
            display_name=display_name,
            model_provider=model_provider,
            model_name=model_name,
            model_version=model_version,
            prompt_version=prompt_version,
        ),
        display_name=display_name,
        model_provider=model_provider,
        model_name=model_name,
        model_version=model_version,
        prompt_version=prompt_version,
    )


@dataclass(slots=True)
class _Counter:
    entity: PublicEntity
    mu: float
    sigma: float
    games: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0

    def to_entry(self) -> PublicLeaderboardEntry:
        return PublicLeaderboardEntry(
            entity=self.entity,
            games=self.games,
            wins=self.wins,
            draws=self.draws,
            losses=self.losses,
            mu=self.mu,
            sigma=self.sigma,
            conservative_score=_conservative(self.mu, self.sigma),
        )


_CACHE: dict[tuple[str | None, str | None, str], PublicLeaderboard] = {}


def reset_cache() -> None:
    """Drop every cached leaderboard. Tests use this to force recomputation."""
    _CACHE.clear()


async def compute_public_leaderboard(
    session: AsyncSession,
    *,
    ruleset_id: str | None = None,
    gauntlet_id: str | None = None,
    provisional_games_threshold: int = DEFAULT_PROVISIONAL_GAMES,
    solo_rate_min_attempts: int = DEFAULT_SOLO_RATE_MIN_ATTEMPTS,
) -> PublicLeaderboard:
    """Return the cached or freshly-computed public leaderboard.

    Cache key includes ``max(ingested_games.created_at)`` for the filter so a
    new submission invalidates the cache naturally — older clients that hold
    the previous ``cache_tag`` simply miss the cache once and get the fresh
    aggregate.
    """
    ingested_tag = await _ingested_cache_part(
        session,
        ruleset_id=ruleset_id,
        gauntlet_id=gauntlet_id,
    )
    canonical_tag = await _rating_cache_part(session, Rating, ruleset_id=ruleset_id)
    placement_tag = await _rating_cache_part(session, PlacementRating, ruleset_id=ruleset_id)
    solo_tag = await _rating_cache_part(session, SoloRateRating, ruleset_id=ruleset_id)
    cache_tag = (
        f"ingested:{ingested_tag};canonical:{canonical_tag};"
        f"placement:{placement_tag};solo:{solo_tag}"
    )
    cache_key: tuple[str | None, str | None, str] = (ruleset_id, gauntlet_id, cache_tag)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    rows: list[IngestedGame] = []
    if ruleset_id is not None:
        rows_stmt = select(IngestedGame).where(
            IngestedGame.ruleset_id == ruleset_id,
            IngestedGame.verification_status == "verified",
        )
        if gauntlet_id is not None:
            rows_stmt = rows_stmt.where(IngestedGame.gauntlet_id == gauntlet_id)
        rows_stmt = rows_stmt.order_by(IngestedGame.created_at, IngestedGame.id)
        rows = list((await session.execute(rows_stmt)).scalars().all())

    entries = _aggregate_entries(
        rows,
        ruleset_id=ruleset_id,
        gauntlet_id=gauntlet_id,
    )
    canonical_cards = await _canonical_cards(
        session,
        ruleset_id=ruleset_id,
        min_games=provisional_games_threshold,
    )
    experimental_cards = await _experimental_cards(
        session,
        ruleset_id=ruleset_id,
        min_games=provisional_games_threshold,
        min_attempts=solo_rate_min_attempts,
    )

    leaderboard = PublicLeaderboard(
        ruleset_id=ruleset_id,
        gauntlet_id=gauntlet_id,
        rating_model=RATING_MODEL,
        cache_tag=cache_tag,
        entries=entries,
        canonical_cards=canonical_cards,
        experimental_cards=experimental_cards,
    )
    _CACHE[cache_key] = leaderboard
    return leaderboard


async def _ingested_cache_part(
    session: AsyncSession,
    *,
    ruleset_id: str | None,
    gauntlet_id: str | None,
) -> str:
    stmt = select(func.max(IngestedGame.created_at), func.count(IngestedGame.id)).where(
        IngestedGame.verification_status == "verified",
    )
    if ruleset_id is not None:
        stmt = stmt.where(IngestedGame.ruleset_id == ruleset_id)
    if gauntlet_id is not None:
        stmt = stmt.where(IngestedGame.gauntlet_id == gauntlet_id)
    max_dt, count = (await session.execute(stmt)).one()
    return _cache_part(max_dt, int(count))


async def _rating_cache_part(
    session: AsyncSession,
    table: type[Rating] | type[PlacementRating] | type[SoloRateRating],
    *,
    ruleset_id: str | None,
) -> str:
    stmt = (
        select(func.max(table.updated_at), func.count(table.id))
        .select_from(table)
        .join(RatingContext, RatingContext.id == table.rating_context_id)
    )
    if ruleset_id is not None:
        stmt = stmt.where(RatingContext.ruleset_id == ruleset_id)
    max_dt, count = (await session.execute(stmt)).one()
    return _cache_part(max_dt, int(count))


def _cache_part(max_dt: datetime | None, count: int) -> str:
    tag = max_dt.isoformat() if max_dt is not None else "empty"
    return f"{tag}:{count}"


def _aggregate_entries(
    rows: Iterable[IngestedGame],
    *,
    ruleset_id: str | None,
    gauntlet_id: str | None,
) -> tuple[PublicLeaderboardEntry, ...]:
    if ruleset_id is None:
        return ()
    return _aggregate(rows, ruleset_id=ruleset_id, gauntlet_id=gauntlet_id).entries


def _aggregate(
    rows: Iterable[IngestedGame],
    *,
    ruleset_id: str,
    gauntlet_id: str | None,
) -> PublicLeaderboard:
    counters: dict[str, _Counter] = {}
    model = PlackettLuce(mu=INITIAL_MU, sigma=INITIAL_SIGMA)

    for row in rows:
        bundle = row.bundle if isinstance(row.bundle, dict) else {}
        outcome = _winner_from_bundle(bundle)
        if outcome is None:
            continue
        seats_by_player, builds_by_player = _index_seats_and_builds(bundle)
        town_entities: list[PublicEntity] = []
        mafia_entities: list[PublicEntity] = []
        for player_id, faction in seats_by_player.items():
            ab = builds_by_player.get(player_id)
            if ab is None:
                continue
            entity = _build_entity(ab)
            if faction == _TOWN:
                town_entities.append(entity)
            elif faction == _MAFIA:
                mafia_entities.append(entity)
        if not town_entities or not mafia_entities:
            continue
        _apply_game(counters, model, town_entities, mafia_entities, outcome)

    entries = sorted(
        (c.to_entry() for c in counters.values()),
        key=lambda e: (-e.conservative_score, e.entity.entity_id),
    )
    return PublicLeaderboard(
        ruleset_id=ruleset_id,
        gauntlet_id=gauntlet_id,
        rating_model=RATING_MODEL,
        cache_tag="legacy",
        entries=tuple(entries),
        canonical_cards=(),
        experimental_cards=(),
    )


async def _agent_entities(
    session: AsyncSession,
    agent_build_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, PublicEntity]:
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
            PromptVersion.version,
        )
        .join(ModelConfig, ModelConfig.id == AgentBuild.model_config_id)
        .join(ModelProvider, ModelProvider.id == ModelConfig.provider_id)
        .join(PromptVersion, PromptVersion.id == AgentBuild.prompt_version_id)
        .where(AgentBuild.id.in_(ids))
    )
    entities: dict[uuid.UUID, PublicEntity] = {}
    for build_id, display_name, provider, model_name, model_version, prompt_version in (
        await session.execute(stmt)
    ).all():
        entities[build_id] = PublicEntity(
            entity_id=_entity_id(
                display_name=str(display_name),
                model_provider=str(provider),
                model_name=str(model_name),
                model_version=model_version,
                prompt_version=str(prompt_version),
            ),
            display_name=str(display_name),
            model_provider=str(provider),
            model_name=str(model_name),
            model_version=model_version,
            prompt_version=str(prompt_version),
        )
    return entities


def _card_id(
    *,
    section: str,
    context_kind: str,
    ruleset_id: str,
    scope_type: str,
    scope_value: str,
    entity_id: str,
) -> str:
    raw = "|".join([section, context_kind, ruleset_id, scope_type, scope_value, entity_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _provisional_reason(kind: str, sample_count: int, minimum: int) -> str | None:
    if sample_count >= minimum:
        return None
    noun = "attempts" if kind == RatingContextKind.SOLO_RATE.value else "games"
    return f"Requires at least {minimum} {noun} in this context; current sample is {sample_count}"


def _openskill_card(
    *,
    section: Literal["canonical", "experimental"],
    section_label: str,
    context: RatingContext,
    entity: PublicEntity,
    scope_type: str,
    scope_value: str,
    mu: float,
    sigma: float,
    conservative_score: float,
    games: int,
    min_games: int,
) -> PublicRatingCard:
    provisional = games < min_games
    metric_label = (
        "Canonical ELO"
        if context.kind == RatingContextKind.CANONICAL_TEAM.value
        else "Placement rating"
    )
    return PublicRatingCard(
        card_id=_card_id(
            section=section,
            context_kind=context.kind,
            ruleset_id=context.ruleset_id,
            scope_type=scope_type,
            scope_value=scope_value,
            entity_id=entity.entity_id,
        ),
        section=section,
        section_label=section_label,
        context_kind=context.kind,
        context_label=context.display_label,
        ruleset_id=context.ruleset_id,
        entity=entity,
        scope_type=scope_type,
        scope_value=scope_value,
        metric="openskill_conservative",
        metric_label=metric_label,
        score=conservative_score,
        rank=None,
        provisional=provisional,
        provisional_reason=_provisional_reason(context.kind, games, min_games),
        sample_count=games,
        games=games,
        attempts=None,
        successes=None,
        mu=mu,
        sigma=sigma,
        conservative_score=conservative_score,
        mean_success_rate=None,
        credible_interval_low=None,
        credible_interval_high=None,
    )


def _solo_card(
    *,
    context: RatingContext,
    entity: PublicEntity,
    scope_type: str,
    scope_value: str,
    successes: int,
    attempts: int,
    mean_success_rate: float,
    min_attempts: int,
) -> PublicRatingCard:
    low, high = beta_binomial_credible_interval(successes, attempts)
    provisional = attempts < min_attempts
    return PublicRatingCard(
        card_id=_card_id(
            section="experimental",
            context_kind=context.kind,
            ruleset_id=context.ruleset_id,
            scope_type=scope_type,
            scope_value=scope_value,
            entity_id=entity.entity_id,
        ),
        section="experimental",
        section_label="Experimental context",
        context_kind=context.kind,
        context_label=context.display_label,
        ruleset_id=context.ruleset_id,
        entity=entity,
        scope_type=scope_type,
        scope_value=scope_value,
        metric="solo_success_rate",
        metric_label="Solo success rate",
        score=mean_success_rate,
        rank=None,
        provisional=provisional,
        provisional_reason=_provisional_reason(context.kind, attempts, min_attempts),
        sample_count=attempts,
        games=None,
        attempts=attempts,
        successes=successes,
        mu=None,
        sigma=None,
        conservative_score=None,
        mean_success_rate=mean_success_rate,
        credible_interval_low=low,
        credible_interval_high=high,
    )


def _rank_cards(cards: Iterable[PublicRatingCard]) -> tuple[PublicRatingCard, ...]:
    grouped: dict[tuple[str, str, str, str, str], list[PublicRatingCard]] = {}
    for card in cards:
        group_key = (
            card.context_kind,
            card.ruleset_id,
            card.scope_type,
            card.scope_value,
            card.metric,
        )
        grouped.setdefault(group_key, []).append(card)

    ranked: list[PublicRatingCard] = []
    for group in grouped.values():
        established = sorted(
            (card for card in group if not card.provisional),
            key=lambda card: (-card.score, card.card_id),
        )
        ranks = {card.card_id: rank for rank, card in enumerate(established, start=1)}
        for card in group:
            ranked.append(replace(card, rank=ranks.get(card.card_id)))

    return tuple(
        sorted(
            ranked,
            key=lambda card: (
                card.context_label,
                card.context_kind,
                card.scope_type,
                card.scope_value,
                card.rank is None,
                card.rank if card.rank is not None else 10**9,
                -card.score,
                card.entity.display_name,
                card.card_id,
            ),
        )
    )


async def _canonical_cards(
    session: AsyncSession,
    *,
    ruleset_id: str | None,
    min_games: int,
) -> tuple[PublicRatingCard, ...]:
    stmt = (
        select(Rating, RatingContext)
        .join(RatingContext, RatingContext.id == Rating.rating_context_id)
        .join(League, League.id == Rating.league_id)
        .where(
            League.kind == LeagueKind.SCIENTIFIC.value,
            RatingContext.kind == RatingContextKind.CANONICAL_TEAM.value,
            RatingContext.is_canonical.is_(True),
            Rating.ruleset_id == RatingContext.ruleset_id,
            Rating.scope_type == SCOPE_GLOBAL,
            Rating.scope_value == SCOPE_VALUE_GLOBAL,
        )
    )
    if ruleset_id is not None:
        stmt = stmt.where(RatingContext.ruleset_id == ruleset_id)
    rows = list((await session.execute(stmt)).all())
    entities = await _agent_entities(session, (row.agent_build_id for row, _context in rows))
    cards: list[PublicRatingCard] = []
    for row, context in rows:
        entity = entities.get(row.agent_build_id)
        if entity is None:
            continue
        cards.append(
            _openskill_card(
                section="canonical",
                section_label="Ranked canonical",
                context=context,
                entity=entity,
                scope_type=row.scope_type,
                scope_value=row.scope_value,
                mu=row.mu,
                sigma=row.sigma,
                conservative_score=row.conservative_score,
                games=row.games,
                min_games=min_games,
            )
        )
    return _rank_cards(cards)


async def _experimental_cards(
    session: AsyncSession,
    *,
    ruleset_id: str | None,
    min_games: int,
    min_attempts: int,
) -> tuple[PublicRatingCard, ...]:
    placement_cards = await _placement_cards(session, ruleset_id=ruleset_id, min_games=min_games)
    solo_cards = await _solo_cards(session, ruleset_id=ruleset_id, min_attempts=min_attempts)
    return _rank_cards((*placement_cards, *solo_cards))


async def _placement_cards(
    session: AsyncSession,
    *,
    ruleset_id: str | None,
    min_games: int,
) -> tuple[PublicRatingCard, ...]:
    stmt = (
        select(PlacementRating, RatingContext)
        .join(RatingContext, RatingContext.id == PlacementRating.rating_context_id)
        .where(
            RatingContext.kind == RatingContextKind.PLACEMENT.value,
            RatingContext.is_canonical.is_(False),
            PlacementRating.scope_type == SCOPE_GLOBAL,
            PlacementRating.scope_value == SCOPE_VALUE_GLOBAL,
        )
    )
    if ruleset_id is not None:
        stmt = stmt.where(RatingContext.ruleset_id == ruleset_id)
    rows = list((await session.execute(stmt)).all())
    entities = await _agent_entities(session, (row.agent_build_id for row, _context in rows))
    cards: list[PublicRatingCard] = []
    for row, context in rows:
        entity = entities.get(row.agent_build_id)
        if entity is None:
            continue
        cards.append(
            _openskill_card(
                section="experimental",
                section_label="Experimental context",
                context=context,
                entity=entity,
                scope_type=row.scope_type,
                scope_value=row.scope_value,
                mu=row.mu,
                sigma=row.sigma,
                conservative_score=row.conservative_score,
                games=row.games,
                min_games=min_games,
            )
        )
    return tuple(cards)


async def _solo_cards(
    session: AsyncSession,
    *,
    ruleset_id: str | None,
    min_attempts: int,
) -> tuple[PublicRatingCard, ...]:
    stmt = (
        select(SoloRateRating, RatingContext)
        .join(RatingContext, RatingContext.id == SoloRateRating.rating_context_id)
        .where(
            RatingContext.kind == RatingContextKind.SOLO_RATE.value,
            RatingContext.is_canonical.is_(False),
        )
    )
    if ruleset_id is not None:
        stmt = stmt.where(RatingContext.ruleset_id == ruleset_id)
    rows = list((await session.execute(stmt)).all())
    entities = await _agent_entities(session, (row.agent_build_id for row, _context in rows))
    cards: list[PublicRatingCard] = []
    for row, context in rows:
        entity = entities.get(row.agent_build_id)
        if entity is None:
            continue
        cards.append(
            _solo_card(
                context=context,
                entity=entity,
                scope_type=row.scope_type,
                scope_value=row.scope_value,
                successes=row.successes,
                attempts=row.attempts,
                mean_success_rate=row.mean_success_rate,
                min_attempts=min_attempts,
            )
        )
    return tuple(cards)


def _winner_from_bundle(bundle: Mapping[str, Any]) -> Literal["TOWN", "MAFIA", "DRAW"] | None:
    terminal = bundle.get("terminal_result")
    if not isinstance(terminal, dict):
        return None
    winner = terminal.get("winner")
    if winner == _TOWN:
        return _TOWN
    if winner == _MAFIA:
        return _MAFIA
    if winner == _DRAW:
        return _DRAW
    return None


def _index_seats_and_builds(
    bundle: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
    seats_by_player: dict[str, str] = {}
    raw_seats = bundle.get("game_seats", [])
    if isinstance(raw_seats, list):
        for seat in raw_seats:
            if not isinstance(seat, dict):
                continue
            pid = seat.get("public_player_id")
            faction = seat.get("faction")
            if isinstance(pid, str) and isinstance(faction, str):
                seats_by_player[pid] = faction
    builds_by_player: dict[str, Mapping[str, Any]] = {}
    raw_builds = bundle.get("agent_builds", [])
    if isinstance(raw_builds, list):
        for ab in raw_builds:
            if not isinstance(ab, dict):
                continue
            pid = ab.get("public_player_id")
            if isinstance(pid, str):
                builds_by_player[pid] = ab
    return seats_by_player, builds_by_player


def _apply_game(
    counters: dict[str, _Counter],
    model: PlackettLuce,
    town: list[PublicEntity],
    mafia: list[PublicEntity],
    winner: Literal["TOWN", "MAFIA", "DRAW"],
) -> None:
    """Apply one openskill update for one terminal game across town vs. mafia."""
    town_counters = [_counter_for(counters, e) for e in town]
    mafia_counters = [_counter_for(counters, e) for e in mafia]

    town_team = [
        model.create_rating([c.mu, c.sigma], name=c.entity.entity_id) for c in town_counters
    ]
    mafia_team = [
        model.create_rating([c.mu, c.sigma], name=c.entity.entity_id) for c in mafia_counters
    ]

    if winner == _TOWN:
        ranks: list[float] = [1.0, 2.0]
    elif winner == _MAFIA:
        ranks = [2.0, 1.0]
    else:
        ranks = [1.0, 1.0]
    new_town, new_mafia = model.rate([town_team, mafia_team], ranks=ranks)

    for c, r in zip(town_counters, new_town, strict=True):
        c.mu = float(r.mu)
        c.sigma = float(r.sigma)
        c.games += 1
        if winner == _TOWN:
            c.wins += 1
        elif winner == _DRAW:
            c.draws += 1
        else:
            c.losses += 1

    for c, r in zip(mafia_counters, new_mafia, strict=True):
        c.mu = float(r.mu)
        c.sigma = float(r.sigma)
        c.games += 1
        if winner == _MAFIA:
            c.wins += 1
        elif winner == _DRAW:
            c.draws += 1
        else:
            c.losses += 1


def _counter_for(counters: dict[str, _Counter], entity: PublicEntity) -> _Counter:
    existing = counters.get(entity.entity_id)
    if existing is not None:
        return existing
    fresh = _Counter(entity=entity, mu=INITIAL_MU, sigma=INITIAL_SIGMA)
    counters[entity.entity_id] = fresh
    return fresh


def entry_to_response(entry: PublicLeaderboardEntry) -> dict[str, Any]:
    """Serialize one entry into the FastAPI response shape."""
    return {
        "entity_id": entry.entity.entity_id,
        "display_name": entry.entity.display_name,
        "model_provider": entry.entity.model_provider,
        "model_name": entry.entity.model_name,
        "model_version": entry.entity.model_version,
        "prompt_version": entry.entity.prompt_version,
        "games": entry.games,
        "wins": entry.wins,
        "draws": entry.draws,
        "losses": entry.losses,
        "mu": entry.mu,
        "sigma": entry.sigma,
        "conservative_score": entry.conservative_score,
    }


def card_to_response(card: PublicRatingCard) -> dict[str, Any]:
    """Serialize one context card into the FastAPI response shape."""
    return {
        "card_id": card.card_id,
        "section": card.section,
        "section_label": card.section_label,
        "context_kind": card.context_kind,
        "context_label": card.context_label,
        "ruleset_id": card.ruleset_id,
        "entity_id": card.entity.entity_id,
        "display_name": card.entity.display_name,
        "model_provider": card.entity.model_provider,
        "model_name": card.entity.model_name,
        "model_version": card.entity.model_version,
        "prompt_version": card.entity.prompt_version,
        "scope_type": card.scope_type,
        "scope_value": card.scope_value,
        "metric": card.metric,
        "metric_label": card.metric_label,
        "score": card.score,
        "rank": card.rank,
        "provisional": card.provisional,
        "provisional_reason": card.provisional_reason,
        "sample_count": card.sample_count,
        "games": card.games,
        "attempts": card.attempts,
        "successes": card.successes,
        "mu": card.mu,
        "sigma": card.sigma,
        "conservative_score": card.conservative_score,
        "mean_success_rate": card.mean_success_rate,
        "credible_interval_low": card.credible_interval_low,
        "credible_interval_high": card.credible_interval_high,
    }


__all__ = [
    "RATING_MODEL",
    "PublicEntity",
    "PublicLeaderboard",
    "PublicLeaderboardEntry",
    "PublicRatingCard",
    "card_to_response",
    "compute_public_leaderboard",
    "entry_to_response",
    "reset_cache",
]
