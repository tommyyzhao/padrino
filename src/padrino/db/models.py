"""SQLAlchemy 2.x ORM models for Padrino's core schema (prd.md §12).

Tables covered: ``model_providers``, ``model_configs``, ``prompt_versions``,
``agent_builds``, ``leagues``, ``gauntlets``, ``gauntlet_roster_slots``,
``games``, ``game_seats``, ``game_events``, ``llm_calls``, ``ratings``,
``rating_events``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from padrino.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_secret_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_providers.id"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    litellm_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    default_temperature: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    default_top_p: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    default_max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    supports_structured_outputs: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    system_prompt: Mapped[str] = mapped_column(String, nullable=False)
    developer_prompt: Mapped[str] = mapped_column(String, nullable=False)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AgentBuild(Base):
    __tablename__ = "agent_builds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_configs.id"), nullable=False
    )
    prompt_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("prompt_versions.id"), nullable=False
    )
    adapter_version: Mapped[str] = mapped_column(String, nullable=False)
    inference_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False, server_default="v1", default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    ranked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Gauntlet(Base):
    __tablename__ = "gauntlets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("prompt_versions.id"), nullable=False
    )
    clone_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gauntlet_seed: Mapped[str] = mapped_column(String, nullable=False)
    ranked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GauntletRosterSlot(Base):
    __tablename__ = "gauntlet_roster_slots"
    __table_args__ = (UniqueConstraint("gauntlet_id", "slot_index", name="uq_gauntlet_slot"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    gauntlet_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("gauntlets.id"), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=False
    )


class Game(Base):
    __tablename__ = "games"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    gauntlet_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("gauntlets.id"), nullable=True
    )
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    game_seed: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    terminal_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    event_hash_head: Mapped[str | None] = mapped_column(String, nullable=True)
    broadcast_state: Mapped[str] = mapped_column(
        String, nullable=False, default="HIDDEN", server_default="HIDDEN"
    )
    is_broadcastable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class GameSeat(Base):
    __tablename__ = "game_seats"
    __table_args__ = (
        UniqueConstraint("game_id", "public_player_id", name="uq_game_seat_public_id"),
        UniqueConstraint("game_id", "seat_index", name="uq_game_seat_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    seat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Nullable since Wave 9 (US-121): a HUMAN seat has no agent build. AI and
    # AI_TAKEOVER seats still populate it (the latter via takeover_agent_build_id
    # for provenance).
    agent_build_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=True
    )
    # Who occupies this seat. 'AI' is the byte-identical legacy default so every
    # pre-Wave-9 game persists/loads unchanged.
    seat_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="AI", server_default="AI"
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    faction: Mapped[str] = mapped_column(String, nullable=False)
    alive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    death_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    # AI takeover provenance (US-122 emits the canonical event; these columns
    # persist the resolved provenance for analytics/reveal).
    taken_over_at_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    takeover_agent_build_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=True
    )


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (UniqueConstraint("game_id", "sequence", name="uq_game_event_sequence"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    visibility: Mapped[str] = mapped_column(String, nullable=False)
    actor_player_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prev_event_hash: Mapped[str] = mapped_column(String, nullable=False)
    event_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("game_events.id"), nullable=True
    )
    # ``agent_build_id`` is nullable in v1: the runner currently runs through a
    # single LlmAdapter without a per-seat build mapping. A later story
    # (gauntlet scheduler) will populate it for ranked runs.
    agent_build_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=True
    )
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    raw_response: Mapped[str | None] = mapped_column(String, nullable=True)
    parsed_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(asdecimal=False), nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (
        UniqueConstraint(
            "league_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            name="uq_rating_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_value: Mapped[str] = mapped_column(String, nullable=False)
    mu: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    sigma: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    conservative_score: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    games: Mapped[int] = mapped_column(Integer, nullable=False)
    last_game_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    submission_public_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestedGame(Base):
    __tablename__ = "ingested_games"
    __table_args__ = (UniqueConstraint("game_id", name="uq_ingested_games_game_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[str] = mapped_column(String, nullable=False)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    league_id: Mapped[str | None] = mapped_column(String, nullable=True)
    gauntlet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tip_hash: Mapped[str] = mapped_column(String, nullable=False)
    signer_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_status: Mapped[str] = mapped_column(String, nullable=False)
    submitter_key_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("api_keys.id"), nullable=True
    )
    bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeats"

    worker_id: Mapped[str] = mapped_column(String, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    key_hash: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class RatingEvent(Base):
    __tablename__ = "rating_events"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            "public_player_id",
            name="uq_rating_event_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=False
    )
    public_player_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_value: Mapped[str] = mapped_column(String, nullable=False)
    before_mu: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    before_sigma: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    after_mu: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    after_sigma: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ScheduledGauntlet(Base):
    """A cron-scheduled recurring heterogeneous tournament (US-085)."""

    __tablename__ = "scheduled_gauntlets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    schedule_cron: Mapped[str] = mapped_column(String, nullable=False)
    # Serialized US-084 roster spec: {"league_id": <uuid>, "roster": {"P01": <uuid>, ...}}.
    roster_spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    n_games: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cost_cap_usd: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_gauntlet_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("gauntlets.id"), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BehavioralEvaluation(Base):
    """Post-game LLM judge behavioral evaluation for a specific player seat (Wave 6)."""

    __tablename__ = "behavioral_evaluations"
    __table_args__ = (
        UniqueConstraint("game_id", "public_player_id", name="uq_behavioral_eval_seat"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id", ondelete="CASCADE"), nullable=False
    )
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    persuasion_score: Mapped[int] = mapped_column(Integer, nullable=False)
    deception_score: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_consistency_score: Mapped[int] = mapped_column(Integer, nullable=False)
    social_heuristics_score: Mapped[int] = mapped_column(Integer, nullable=False)
    written_feedback: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AnalyticsAggregate(Base):
    """Per-agent analytics aggregate keyed by (ruleset_id, agent_build_id, version) (US-102).

    Materialized by ``padrino.analytics.deterministic.compute_game_analytics``
    rolled up across all games an agent participated in.  JSON columns store
    serialized ``RoleWinRate`` and ``SurvivalPoint`` lists.
    """

    __tablename__ = "analytics_aggregates"
    __table_args__ = (
        UniqueConstraint("ruleset_id", "agent_build_id", "version", name="uq_analytics_aggregate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False)
    role_win_rates_json: Mapped[str] = mapped_column(String, nullable=False)
    voting_total_votes: Mapped[int] = mapped_column(Integer, nullable=False)
    voting_accurate_votes: Mapped[int] = mapped_column(Integer, nullable=False)
    survival_curve_json: Mapped[str] = mapped_column(String, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MaterializedGameAnalytics(Base):
    """Per-game deterministic analytics + claim analysis, computed once at RECENT (US-120).

    Keyed by ``game_id`` (one row per game).  The stored ``analytics_json`` is
    the full, outcome-revealing ``PublicGameAnalyticsResponse`` payload (winner
    and role_win_rates included) — it is only served for RECENT games, whose
    outcome is already public, so persisting the spoiler fields is safe.  LIVE
    games keep the existing on-the-fly spoiler-safe path and never read this row.
    Materialized on ``mark_recent`` instead of re-deriving the full event log on
    every recap request.
    """

    __tablename__ = "materialized_game_analytics"

    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    analytics_json: Mapped[str] = mapped_column(String, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class JudgeEnrichmentCard(Base):
    """Per-agent-role judge enrichment trend card aggregated from BehavioralEvaluation rows (US-105).

    Keyed by (agent_build_id, role, ruleset_id).  Stores average judge dimension
    scores across all evaluated games the agent played in the given role.
    Clearly separate from rating tables (Rating, RatingEvent) — judge output
    never writes a Rating row.
    """

    __tablename__ = "judge_enrichment_cards"
    __table_args__ = (
        UniqueConstraint("agent_build_id", "role", "ruleset_id", name="uq_judge_enrichment_card"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    games_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_persuasion: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    avg_deception: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    avg_logical_consistency: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    avg_social_heuristics: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "AgentBuild",
    "AnalyticsAggregate",
    "ApiKey",
    "BehavioralEvaluation",
    "Game",
    "GameEvent",
    "GameSeat",
    "Gauntlet",
    "GauntletRosterSlot",
    "IngestedGame",
    "JudgeEnrichmentCard",
    "League",
    "LlmCall",
    "MaterializedGameAnalytics",
    "ModelConfig",
    "ModelProvider",
    "PromptVersion",
    "RateLimitBucket",
    "Rating",
    "RatingEvent",
    "ScheduledGauntlet",
    "SchedulerHeartbeat",
]
