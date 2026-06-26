"""Campaign publish-gate checklist for uncertainty-based benchmark publication."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.event_log import StoredEvent
from padrino.core.engine.hashing import GENESIS_HASH
from padrino.core.engine.replay import ReplayHashMismatchError, replay_event_log
from padrino.core.enums import RatingContextKind
from padrino.core.observation_privacy import FORBIDDEN_PAYLOAD_KEYS
from padrino.db.game_status import GAME_STATUS_COMPLETED
from padrino.db.models import (
    AgentBuild,
    Campaign,
    CampaignPairing,
    Game,
    GameEvent,
    GameSeat,
    Gauntlet,
    LlmCall,
    ModelConfig,
    ModelProvider,
    Rating,
    RatingContext,
    RatingEvent,
)
from padrino.db.repositories import campaigns as campaigns_repo
from padrino.economics.human_cost_governance import (
    PRICE_BASIS_FALLBACK_TABLE,
    PRICE_BASIS_PROVIDER_RESPONSE_COST,
)
from padrino.gauntlets.campaign_report import CampaignReportNotFound, build_campaign_report
from padrino.ratings.model_rollup import model_key_for
from padrino.ratings.openskill_service import SCOPE_FACTION, SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL

PublishGateBlockerCode = Literal[
    "campaign_not_found",
    "canonical_rating_missing",
    "cost_stamp_missing",
    "excluded_rating_event",
    "identity_leak",
    "model_under_sampled",
    "rank_unstable",
    "replay_hash",
    "scope_provisional",
    "sigma_high",
]

_TERMINAL_REVEAL_KEYS = frozenset({"role", "faction"})
_PUBLISH_FORBIDDEN_PAYLOAD_KEYS = FORBIDDEN_PAYLOAD_KEYS - _TERMINAL_REVEAL_KEYS


class PublishGateBlocker(BaseModel):
    """One itemized reason a campaign is not ready for public publication."""

    model_config = ConfigDict(frozen=True)

    code: PublishGateBlockerCode
    message: str
    game_id: uuid.UUID | None = None
    scope_kind: str | None = None
    entity_id: str | None = None
    entity_label: str | None = None


class PublishGateDocumentedHole(BaseModel):
    """One dead-lettered campaign cell documented beside the gate result."""

    model_config = ConfigDict(frozen=True)

    cell_id: uuid.UUID
    cell_index: int
    gauntlet_id: uuid.UUID | None
    attempt_count: int
    last_error: str | None
    last_error_kind: str | None


class PublishGateResult(BaseModel):
    """Ready-to-publish decision plus blockers and non-blocking holes."""

    model_config = ConfigDict(frozen=True)

    campaign_id: uuid.UUID
    ready_to_publish: bool
    blockers: tuple[PublishGateBlocker, ...]
    documented_holes: tuple[PublishGateDocumentedHole, ...]


@dataclass(frozen=True, slots=True)
class _ModelScope:
    key: str
    label: str
    agent_build_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class _RatingSample:
    agent_build_id: uuid.UUID
    mu: float
    sigma: float
    games: int


@dataclass(frozen=True, slots=True)
class _ModelConvergence:
    scope: _ModelScope
    games: int
    rating_games: int
    sigma: float
    score: float
    rank_stable: bool | None
    rank_delta: int | None


def _blocker(
    code: PublishGateBlockerCode,
    message: str,
    *,
    game_id: uuid.UUID | None = None,
    scope_kind: str | None = None,
    entity_id: str | None = None,
    entity_label: str | None = None,
) -> PublishGateBlocker:
    return PublishGateBlocker(
        code=code,
        message=message,
        game_id=game_id,
        scope_kind=scope_kind,
        entity_id=entity_id,
        entity_label=entity_label,
    )


async def evaluate_publish_gate(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> PublishGateResult:
    """Evaluate whether ``campaign_id`` is ready for public benchmark publication."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignReportNotFound(f"campaign {campaign_id} not found")

    report = await build_campaign_report(
        session,
        campaign_id,
        provisional_games_threshold=campaign.per_model_game_target,
    )
    documented_holes = tuple(
        PublishGateDocumentedHole(
            cell_id=hole.cell_id,
            cell_index=hole.cell_index,
            gauntlet_id=hole.gauntlet_id,
            attempt_count=hole.attempt_count,
            last_error=hole.last_error,
            last_error_kind=hole.last_error_kind,
        )
        for hole in report.dead_letters
    )

    blockers: list[PublishGateBlocker] = []
    blockers.extend(await _replay_hash_blockers(session, campaign_id))
    published_game_ids = await _published_game_ids(session, campaign_id)
    blockers.extend(await _identity_blockers(session, published_game_ids))
    blockers.extend(await _excluded_rating_blockers(session, campaign))
    blockers.extend(await _cost_stamp_blockers(session, published_game_ids))
    blockers.extend(await _canonical_model_blockers(session, campaign))
    blockers.extend(_reported_scope_blockers(report.convergence, campaign.sigma_target))

    blockers = _dedupe_blockers(blockers)
    return PublishGateResult(
        campaign_id=campaign_id,
        ready_to_publish=not blockers,
        blockers=tuple(blockers),
        documented_holes=documented_holes,
    )


def _dedupe_blockers(blockers: Iterable[PublishGateBlocker]) -> list[PublishGateBlocker]:
    seen: set[tuple[str, str, uuid.UUID | None]] = set()
    out: list[PublishGateBlocker] = []
    for blocker in blockers:
        key = (blocker.code, blocker.message, blocker.game_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(blocker)
    return out


async def _campaign_games(session: AsyncSession, campaign_id: uuid.UUID) -> list[Game]:
    stmt = (
        select(Game)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .where(Gauntlet.campaign_id == campaign_id)
        .order_by(Game.created_at, Game.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _published_game_ids(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    stmt = (
        select(Game.id)
        .select_from(Game)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .join(CampaignPairing, CampaignPairing.gauntlet_id == Gauntlet.id)
        .where(
            CampaignPairing.campaign_id == campaign_id,
            CampaignPairing.status == campaigns_repo.CAMPAIGN_PAIRING_COMPLETED,
            Game.status == GAME_STATUS_COMPLETED,
            Game.terminal_result.is_not(None),
        )
        .order_by(Game.created_at, Game.id)
    )
    return tuple((await session.execute(stmt)).scalars().all())


def _event_body(event: GameEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "sequence": event.sequence,
        "phase": event.phase,
        "visibility": event.visibility,
        "actor_player_id": event.actor_player_id,
        "payload": event.payload,
    }


async def _replay_hash_blockers(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> list[PublishGateBlocker]:
    blockers: list[PublishGateBlocker] = []
    for game in await _campaign_games(session, campaign_id):
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game.id)
                    .order_by(GameEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            continue

        previous_hash = GENESIS_HASH
        sequence_error: str | None = None
        for expected_sequence, row in enumerate(rows):
            if row.sequence != expected_sequence:
                sequence_error = (
                    f"game {game.id} hash verification failed: event sequence "
                    f"{row.sequence} is not contiguous at {expected_sequence}"
                )
                break
            if row.prev_event_hash != previous_hash:
                sequence_error = (
                    f"game {game.id} hash verification failed: event {row.sequence} "
                    "does not chain from previous hash"
                )
                break
            previous_hash = row.event_hash
        if sequence_error is not None:
            blockers.append(_blocker("replay_hash", sequence_error, game_id=game.id))
            continue

        stored = [
            StoredEvent(
                sequence=row.sequence,
                prev_event_hash=row.prev_event_hash,
                event_hash=row.event_hash,
                body=_event_body(row),
            )
            for row in rows
        ]
        try:
            replay_event_log(stored)
        except ReplayHashMismatchError as exc:
            blockers.append(
                _blocker(
                    "replay_hash",
                    (
                        f"game {game.id} hash verification failed at sequence "
                        f"{exc.sequence}: expected {exc.expected}, got {exc.actual}"
                    ),
                    game_id=game.id,
                )
            )
    return blockers


async def _identity_blockers(
    session: AsyncSession,
    game_ids: tuple[uuid.UUID, ...],
) -> list[PublishGateBlocker]:
    if not game_ids:
        return []
    rows = list(
        (
            await session.execute(
                select(GameEvent)
                .where(
                    GameEvent.game_id.in_(game_ids),
                    GameEvent.visibility == "PUBLIC",
                )
                .order_by(GameEvent.game_id, GameEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    blockers: list[PublishGateBlocker] = []
    for row in rows:
        for key, path in _forbidden_payload_findings(row.payload):
            blockers.append(
                _blocker(
                    "identity_leak",
                    (
                        f"game {row.game_id} event {row.sequence} leaks forbidden "
                        f"key {key!r} at {path}"
                    ),
                    game_id=row.game_id,
                )
            )
    return blockers


def _forbidden_payload_findings(
    value: Any,
    *,
    path: str = "payload",
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, sub_value in value.items():
            key_str = str(key)
            sub_path = f"{path}.{key_str}"
            if key_str in _PUBLISH_FORBIDDEN_PAYLOAD_KEYS:
                findings.append((key_str, sub_path))
            findings.extend(_forbidden_payload_findings(sub_value, path=sub_path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings.extend(_forbidden_payload_findings(item, path=f"{path}[{index}]"))
    return findings


async def _cost_stamp_blockers(
    session: AsyncSession,
    game_ids: tuple[uuid.UUID, ...],
) -> list[PublishGateBlocker]:
    if not game_ids:
        return []
    rows = list(
        (
            await session.execute(
                select(LlmCall)
                .where(LlmCall.game_id.in_(game_ids))
                .order_by(LlmCall.game_id, LlmCall.created_at, LlmCall.id)
            )
        )
        .scalars()
        .all()
    )
    blockers: list[PublishGateBlocker] = []
    for call in rows:
        if call.cost_usd is None and call.price_basis is None:
            continue
        if call.cost_usd is None:
            blockers.append(
                _blocker(
                    "cost_stamp_missing",
                    f"llm_call {call.id} for game {call.game_id} missing cost_usd",
                    game_id=call.game_id,
                )
            )
        if call.price_basis is None:
            blockers.append(
                _blocker(
                    "cost_stamp_missing",
                    f"llm_call {call.id} for game {call.game_id} missing price_basis",
                    game_id=call.game_id,
                )
            )
            continue
        if call.price_basis == PRICE_BASIS_FALLBACK_TABLE and not call.price_table_version:
            blockers.append(
                _blocker(
                    "cost_stamp_missing",
                    (
                        f"llm_call {call.id} for game {call.game_id} "
                        "missing fallback price_table_version"
                    ),
                    game_id=call.game_id,
                )
            )
        elif call.price_basis not in (
            PRICE_BASIS_FALLBACK_TABLE,
            PRICE_BASIS_PROVIDER_RESPONSE_COST,
        ):
            blockers.append(
                _blocker(
                    "cost_stamp_missing",
                    (
                        f"llm_call {call.id} for game {call.game_id} has unknown "
                        f"price_basis {call.price_basis!r}"
                    ),
                    game_id=call.game_id,
                )
            )
    return blockers


async def _excluded_rating_blockers(
    session: AsyncSession,
    campaign: Campaign,
) -> list[PublishGateBlocker]:
    context = await _canonical_context(session, campaign)
    if context is None:
        return []
    stmt = (
        select(
            RatingEvent.game_id,
            Game.status,
            CampaignPairing.cell_index,
            CampaignPairing.status,
        )
        .select_from(RatingEvent)
        .join(Game, Game.id == RatingEvent.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .join(CampaignPairing, CampaignPairing.gauntlet_id == Gauntlet.id)
        .where(
            CampaignPairing.campaign_id == campaign.id,
            RatingEvent.rating_context_id == context.id,
            or_(
                Game.status != GAME_STATUS_COMPLETED,
                CampaignPairing.status != campaigns_repo.CAMPAIGN_PAIRING_COMPLETED,
            ),
        )
        .order_by(CampaignPairing.cell_index, RatingEvent.game_id)
    )
    blockers: list[PublishGateBlocker] = []
    for game_id, game_status, cell_index, cell_status in (await session.execute(stmt)).all():
        blockers.append(
            _blocker(
                "excluded_rating_event",
                (
                    f"excluded game {game_id} from cell {cell_index} has a canonical "
                    f"rating event (game_status={game_status}, cell_status={cell_status})"
                ),
                game_id=game_id,
            )
        )
    return blockers


async def _canonical_context(session: AsyncSession, campaign: Campaign) -> RatingContext | None:
    stmt = (
        select(RatingContext)
        .join(Rating, Rating.rating_context_id == RatingContext.id)
        .where(
            Rating.league_id == campaign.league_id,
            RatingContext.ruleset_id == campaign.ruleset_id,
            RatingContext.kind == RatingContextKind.CANONICAL_TEAM.value,
            RatingContext.is_canonical.is_(True),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _canonical_model_blockers(
    session: AsyncSession,
    campaign: Campaign,
) -> list[PublishGateBlocker]:
    expected = await _expected_model_scopes(session, campaign.id)
    if not expected:
        return [
            _blocker(
                "model_under_sampled",
                f"campaign {campaign.id} has no roster models to publish",
            )
        ]
    coverage = await _published_model_coverage(session, campaign.id)
    convergence = await _canonical_model_convergence(session, campaign, expected, coverage)

    blockers: list[PublishGateBlocker] = []
    target = campaign.per_model_game_target
    for key, scope in sorted(expected.items()):
        games = coverage.get(key, 0)
        if games < target:
            blockers.append(
                _blocker(
                    "model_under_sampled",
                    f"model {scope.label} under-sampled: {games} < {target}",
                    scope_kind="model",
                    entity_id=key,
                    entity_label=scope.label,
                )
            )
        item = convergence.get(key)
        if item is None:
            blockers.append(
                _blocker(
                    "canonical_rating_missing",
                    f"model {scope.label} missing canonical rating",
                    scope_kind="model",
                    entity_id=key,
                    entity_label=scope.label,
                )
            )
            continue
        if item.rating_games < target:
            blockers.append(
                _blocker(
                    "scope_provisional",
                    (
                        f"model {scope.label} canonical rating provisional: "
                        f"{item.rating_games} < {target}"
                    ),
                    scope_kind="model",
                    entity_id=key,
                    entity_label=scope.label,
                )
            )
        if item.sigma > campaign.sigma_target:
            blockers.append(
                _blocker(
                    "sigma_high",
                    f"model {scope.label} sigma {item.sigma:.3f} > target {campaign.sigma_target:.3f}",
                    scope_kind="model",
                    entity_id=key,
                    entity_label=scope.label,
                )
            )
        if item.rank_stable is not True:
            detail = "unavailable" if item.rank_delta is None else f"moved by {item.rank_delta}"
            blockers.append(
                _blocker(
                    "rank_unstable",
                    (
                        f"model {scope.label} rank {detail} over last "
                        f"{campaign.rank_stability_k} updates"
                    ),
                    scope_kind="model",
                    entity_id=key,
                    entity_label=scope.label,
                )
            )
    return blockers


async def _expected_model_scopes(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> dict[str, _ModelScope]:
    rows = (
        await session.execute(
            select(CampaignPairing.roster_json).where(CampaignPairing.campaign_id == campaign_id)
        )
    ).scalars()
    build_ids: set[uuid.UUID] = set()
    for roster in rows:
        for raw_id in roster:
            try:
                build_ids.add(uuid.UUID(str(raw_id)))
            except ValueError:
                continue
    return await _model_scopes_from_build_ids(session, build_ids)


async def _model_scopes_from_build_ids(
    session: AsyncSession,
    build_ids: Iterable[uuid.UUID],
) -> dict[str, _ModelScope]:
    ids = list(dict.fromkeys(build_ids))
    if not ids:
        return {}
    stmt = (
        select(
            AgentBuild.id,
            ModelProvider.name,
            ModelConfig.model_name,
            ModelConfig.model_version,
        )
        .join(ModelConfig, ModelConfig.id == AgentBuild.model_config_id)
        .join(ModelProvider, ModelProvider.id == ModelConfig.provider_id)
        .where(AgentBuild.id.in_(ids))
    )
    grouped: dict[str, tuple[str, set[uuid.UUID]]] = {}
    for build_id, provider, model_name, model_version in (await session.execute(stmt)).all():
        key = model_key_for(str(provider), str(model_name), model_version)
        label = str(model_name) if not model_version else f"{model_name} @{model_version}"
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = (label, {build_id})
        else:
            existing[1].add(build_id)
    return {
        key: _ModelScope(key=key, label=label, agent_build_ids=frozenset(build_ids))
        for key, (label, build_ids) in grouped.items()
    }


async def _published_model_coverage(
    session: AsyncSession,
    campaign_id: uuid.UUID,
) -> dict[str, int]:
    stmt = (
        select(GameSeat.agent_build_id, func.count(GameSeat.id))
        .select_from(GameSeat)
        .join(Game, Game.id == GameSeat.game_id)
        .join(Gauntlet, Gauntlet.id == Game.gauntlet_id)
        .join(CampaignPairing, CampaignPairing.gauntlet_id == Gauntlet.id)
        .where(
            CampaignPairing.campaign_id == campaign_id,
            CampaignPairing.status == campaigns_repo.CAMPAIGN_PAIRING_COMPLETED,
            Game.status == GAME_STATUS_COMPLETED,
            Game.terminal_result.is_not(None),
            GameSeat.agent_build_id.is_not(None),
        )
        .group_by(GameSeat.agent_build_id)
    )
    rows = [(build_id, int(count)) for build_id, count in (await session.execute(stmt)).all()]
    scopes = await _model_scopes_from_build_ids(session, (build_id for build_id, _count in rows))
    counts: dict[str, int] = defaultdict(int)
    for build_id, count in rows:
        for key, scope in scopes.items():
            if build_id in scope.agent_build_ids:
                counts[key] += count
                break
    return dict(counts)


async def _canonical_model_convergence(
    session: AsyncSession,
    campaign: Campaign,
    expected: Mapping[str, _ModelScope],
    coverage: Mapping[str, int],
) -> dict[str, _ModelConvergence]:
    context = await _canonical_context(session, campaign)
    if context is None:
        return {}

    stmt = (
        select(Rating)
        .join(RatingContext, RatingContext.id == Rating.rating_context_id)
        .where(
            Rating.league_id == campaign.league_id,
            Rating.ruleset_id == campaign.ruleset_id,
            RatingContext.id == context.id,
            Rating.scope_type == SCOPE_GLOBAL,
            Rating.scope_value == SCOPE_VALUE_GLOBAL,
            Rating.agent_build_id.in_(
                [build_id for scope in expected.values() for build_id in scope.agent_build_ids]
            ),
        )
    )
    by_model: dict[str, list[_RatingSample]] = defaultdict(list)
    build_to_model: dict[uuid.UUID, str] = {}
    for row in (await session.execute(stmt)).scalars().all():
        for key, scope in expected.items():
            if row.agent_build_id in scope.agent_build_ids:
                by_model[key].append(
                    _RatingSample(
                        agent_build_id=row.agent_build_id,
                        mu=float(row.mu),
                        sigma=float(row.sigma),
                        games=int(row.games),
                    )
                )
                build_to_model[row.agent_build_id] = key
                break

    current_scores: dict[str, float] = {}
    sigmas: dict[str, float] = {}
    rating_games: dict[str, int] = {}
    for key, samples in by_model.items():
        mu, sigma = _aggregate_rating(samples)
        current_scores[key] = _score(mu, sigma)
        sigmas[key] = sigma
        rating_games[key] = sum(sample.games for sample in samples)

    rank_stability = await _model_rank_stability(
        session,
        campaign=campaign,
        context=context,
        current_scores=current_scores,
        build_to_model=build_to_model,
    )
    return {
        key: _ModelConvergence(
            scope=expected[key],
            games=coverage.get(key, 0),
            rating_games=rating_games[key],
            sigma=sigmas[key],
            score=current_scores[key],
            rank_stable=rank_stability.get(key, (None, None))[0],
            rank_delta=rank_stability.get(key, (None, None))[1],
        )
        for key in by_model
    }


def _aggregate_rating(samples: Iterable[_RatingSample]) -> tuple[float, float]:
    weighted = [sample for sample in samples if sample.games > 0]
    if not weighted:
        return 25.0, 25.0 / 3.0
    total = sum(sample.games for sample in weighted)
    mu = sum(sample.mu * sample.games for sample in weighted) / total
    sigma = math.sqrt(sum((sample.sigma * sample.games) ** 2 for sample in weighted)) / total
    return mu, sigma


def _score(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


def _ranks(scores: Mapping[str, float]) -> dict[str, int]:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {entity_id: rank for rank, (entity_id, _score_value) in enumerate(ordered, start=1)}


async def _model_rank_stability(
    session: AsyncSession,
    *,
    campaign: Campaign,
    context: RatingContext,
    current_scores: Mapping[str, float],
    build_to_model: Mapping[uuid.UUID, str],
) -> dict[str, tuple[bool | None, int | None]]:
    if campaign.rank_stability_k <= 0:
        return dict.fromkeys(current_scores, (True, 0))
    stmt = (
        select(RatingEvent)
        .where(
            RatingEvent.league_id == campaign.league_id,
            RatingEvent.ruleset_id == campaign.ruleset_id,
            RatingEvent.rating_context_id == context.id,
            RatingEvent.scope_type == SCOPE_GLOBAL,
            RatingEvent.scope_value == SCOPE_VALUE_GLOBAL,
        )
        .order_by(desc(RatingEvent.created_at), desc(RatingEvent.id))
        .limit(campaign.rank_stability_k)
    )
    baseline = dict(current_scores)
    for event in (await session.execute(stmt)).scalars().all():
        model_key = build_to_model.get(event.agent_build_id)
        if model_key is None:
            continue
        after = _score(float(event.after_mu), float(event.after_sigma))
        before = _score(float(event.before_mu), float(event.before_sigma))
        baseline[model_key] = baseline.get(model_key, 0.0) - (after - before)

    current_ranks = _ranks(current_scores)
    earliest_ranks = _ranks(baseline)
    out: dict[str, tuple[bool | None, int | None]] = {}
    for key in current_scores:
        current_rank = current_ranks.get(key)
        earliest_rank = earliest_ranks.get(key)
        if current_rank is None or earliest_rank is None:
            out[key] = (None, None)
            continue
        rank_delta = abs(current_rank - earliest_rank)
        out[key] = (rank_delta == 0, rank_delta)
    return out


def _reported_scope_blockers(
    convergence: Iterable[Any],
    sigma_target: float,
) -> list[PublishGateBlocker]:
    blockers: list[PublishGateBlocker] = []
    for item in convergence:
        if item.scope_kind == "model":
            continue
        if item.scope_type not in (SCOPE_GLOBAL, SCOPE_FACTION):
            continue
        if item.provisional:
            blockers.append(
                _blocker(
                    "scope_provisional",
                    (
                        f"scope {item.entity_label} {item.scope_type}/{item.scope_value} "
                        f"provisional with {item.games} games"
                    ),
                    scope_kind=item.scope_kind,
                    entity_id=item.entity_id,
                    entity_label=item.entity_label,
                )
            )
        if item.sigma > sigma_target:
            blockers.append(
                _blocker(
                    "sigma_high",
                    (
                        f"scope {item.entity_label} {item.scope_type}/{item.scope_value} "
                        f"sigma {item.sigma:.3f} > target {sigma_target:.3f}"
                    ),
                    scope_kind=item.scope_kind,
                    entity_id=item.entity_id,
                    entity_label=item.entity_label,
                )
            )
        if item.rank_stability.stable is not True:
            detail = (
                "unavailable"
                if item.rank_stability.rank_delta is None
                else f"moved by {item.rank_stability.rank_delta}"
            )
            blockers.append(
                _blocker(
                    "rank_unstable",
                    (
                        f"scope {item.entity_label} {item.scope_type}/{item.scope_value} "
                        f"rank {detail} over last {item.rank_stability.window_size} updates"
                    ),
                    scope_kind=item.scope_kind,
                    entity_id=item.entity_id,
                    entity_label=item.entity_label,
                )
            )
    return blockers


__all__ = [
    "PublishGateBlocker",
    "PublishGateDocumentedHole",
    "PublishGateResult",
    "evaluate_publish_gate",
]
