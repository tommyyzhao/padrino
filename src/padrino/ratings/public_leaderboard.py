"""Federated public-leaderboard aggregation over ingested bundles (US-063).

Computes an openskill rollup across every row in ``ingested_games`` matching
``ruleset_id`` (and optionally ``gauntlet_id``), keyed by the
``(display_name, model_provider, model_name, model_version, prompt_version)``
tuple that the export bundle's :class:`AgentBuildInfo` already carries.

The leaderboard is recomputed on read, but cached behind
``max(ingested_games.created_at)`` — new ingestions invalidate the cache
naturally because the tag changes the moment a fresh row lands. The cache is
per process and intentionally simple: a single dict keyed by
``(ruleset_id, gauntlet_id, cache_tag)``.

Entries are openly comparable: there is no submitter-scoped data in the
returned shape, only the public AgentBuildInfo fields and the openskill
result. The ``GET /public/leaderboard`` route layers pagination on top.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from openskill.models import PlackettLuce
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import IngestedGame
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA

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
class PublicLeaderboard:
    ruleset_id: str
    gauntlet_id: str | None
    rating_model: str
    cache_tag: str
    entries: tuple[PublicLeaderboardEntry, ...]


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


_CACHE: dict[tuple[str, str | None, str], PublicLeaderboard] = {}


def reset_cache() -> None:
    """Drop every cached leaderboard. Tests use this to force recomputation."""
    _CACHE.clear()


async def compute_public_leaderboard(
    session: AsyncSession,
    *,
    ruleset_id: str,
    gauntlet_id: str | None = None,
) -> PublicLeaderboard:
    """Return the cached or freshly-computed public leaderboard.

    Cache key includes ``max(ingested_games.created_at)`` for the filter so a
    new submission invalidates the cache naturally — older clients that hold
    the previous ``cache_tag`` simply miss the cache once and get the fresh
    aggregate.
    """
    tag_stmt = select(func.max(IngestedGame.created_at)).where(
        IngestedGame.ruleset_id == ruleset_id
    )
    if gauntlet_id is not None:
        tag_stmt = tag_stmt.where(IngestedGame.gauntlet_id == gauntlet_id)
    max_dt: datetime | None = (await session.execute(tag_stmt)).scalar_one_or_none()
    cache_tag = max_dt.isoformat() if max_dt is not None else "empty"
    cache_key: tuple[str, str | None, str] = (ruleset_id, gauntlet_id, cache_tag)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    rows_stmt = select(IngestedGame).where(IngestedGame.ruleset_id == ruleset_id)
    if gauntlet_id is not None:
        rows_stmt = rows_stmt.where(IngestedGame.gauntlet_id == gauntlet_id)
    rows_stmt = rows_stmt.order_by(IngestedGame.created_at, IngestedGame.id)
    rows = list((await session.execute(rows_stmt)).scalars().all())

    leaderboard = _aggregate(
        rows,
        ruleset_id=ruleset_id,
        gauntlet_id=gauntlet_id,
        cache_tag=cache_tag,
    )
    _CACHE[cache_key] = leaderboard
    return leaderboard


def _aggregate(
    rows: Iterable[IngestedGame],
    *,
    ruleset_id: str,
    gauntlet_id: str | None,
    cache_tag: str,
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
        cache_tag=cache_tag,
        entries=tuple(entries),
    )


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


__all__ = [
    "RATING_MODEL",
    "PublicEntity",
    "PublicLeaderboard",
    "PublicLeaderboardEntry",
    "compute_public_leaderboard",
    "entry_to_response",
    "reset_cache",
]
