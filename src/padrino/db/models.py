"""SQLAlchemy 2.x ORM models for Padrino's core schema (prd.md §12).

Tables covered: ``model_providers``, ``model_configs``, ``prompt_versions``,
``agent_builds``, ``leagues``, ``gauntlets``, ``gauntlet_roster_slots``,
``games``, ``game_seats``, ``game_events``, ``llm_calls``, ``ratings``,
``rating_events``, the dormant human-lane siblings ``human_rating`` /
``human_rating_event`` (Wave 9, US-125), and the browser-human identity layer
``principals`` / ``human_sessions`` (Wave 9, US-127), plus human-lane cost
admission slots.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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
    # Discriminator (Wave 9): SCIENTIFIC owns the sacred Rating tables;
    # HUMANS_INCLUDED is the dormant casual human lane. Server-defaults to
    # SCIENTIFIC so every existing league row is byte-identical after upgrade.
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="SCIENTIFIC", server_default="SCIENTIFIC"
    )
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
    # Wave 9 (US-126): per-game human-vs-AI / model-identity disclosure mode.
    # 'ANONYMOUS' is the byte-identical legacy default (and the fail-closed
    # value) so every pre-Wave-9 game persists/loads unchanged; frozen after
    # game start.
    identity_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="ANONYMOUS", server_default="ANONYMOUS"
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
    # Wave 9 (US-127): a HUMAN seat links to the human principal occupying it.
    # Nullable so AI / AI_TAKEOVER seats (and every legacy row) stay byte-identical.
    occupant_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("principals.id"), nullable=True
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
    # Who funds this inference (US-151). 'PLATFORM' is the byte-identical default
    # (human play is platform-absorbed in v1); BYOK_OWNER / SPONSOR_POOL are
    # designed-now-dormant so the cost-tracking row is forward-compatible.
    funding_source: Mapped[str] = mapped_column(
        String, nullable=False, default="PLATFORM", server_default="PLATFORM"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class HumanCostAdmission(Base):
    """Finite per-principal/day admission slot claimed before human-lane writes."""

    __tablename__ = "human_cost_admissions"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "admission_day",
            "bucket",
            "slot_index",
            name="uq_human_cost_admission_slot",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    admission_day: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[str] = mapped_column(String, nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(
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


class HumanRating(Base):
    """Dormant sibling of :class:`Rating` for the humans-included league (US-125).

    Mirrors the scientific ``ratings`` row shape but is keyed by
    ``human_player_id`` (a human principal reference) instead of an agent build,
    and is NEVER written in v1 (casual). It exists so future activation of human
    ELO is a flag-flip, not a migration. Writing a human-lane game touches
    neither this table nor the scientific ``ratings`` table in v1.
    """

    __tablename__ = "human_rating"
    __table_args__ = (
        UniqueConstraint(
            "league_id",
            "human_player_id",
            "scope_type",
            "scope_value",
            name="uq_human_rating_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    human_player_id: Mapped[str] = mapped_column(String, nullable=False)
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


class HumanRatingEvent(Base):
    """Dormant sibling of :class:`RatingEvent` for the humans-included league (US-125).

    Mirrors the scientific ``rating_events`` audit-row shape but is keyed by
    ``human_player_id`` instead of an agent build. NEVER written in v1.
    """

    __tablename__ = "human_rating_event"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "human_player_id",
            "scope_type",
            "scope_value",
            name="uq_human_rating_event_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    human_player_id: Mapped[str] = mapped_column(String, nullable=False)
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


class HumanPlayerStats(Base):
    """Per-human deterministic play-history aggregate keyed by (ruleset_id, principal_id) (US-145).

    Materialized by
    :func:`padrino.analytics.deterministic.compute_participant_stats` rolled up
    across every COMPLETED human-lane game (a seat the principal occupied) of the
    ruleset.  This is the casual humans-included surface only: it is NEVER written
    for scientific-league (AI-only) games, and there is NO leaderboard or ELO in
    v1 (hard rule 8 — the dormant ``human_rating`` table stays empty).

    Counts (not floats) are persisted so re-running a recompute is idempotent
    under the ``(ruleset_id, principal_id)`` unique constraint and so rates are
    derived on read.  ``role_win_rates_json`` /  ``faction_win_rates_json`` store
    serialized ``{name, wins, games}`` lists.
    """

    __tablename__ = "human_player_stats"
    __table_args__ = (UniqueConstraint("ruleset_id", "principal_id", name="uq_human_player_stats"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    games: Mapped[int] = mapped_column(Integer, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, nullable=False)
    role_win_rates_json: Mapped[str] = mapped_column(String, nullable=False)
    faction_win_rates_json: Mapped[str] = mapped_column(String, nullable=False)
    survived_games: Mapped[int] = mapped_column(Integer, nullable=False)
    voting_total_votes: Mapped[int] = mapped_column(Integer, nullable=False)
    voting_accurate_votes: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_total: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_accurate: Mapped[int] = mapped_column(Integer, nullable=False)
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


class HumanChatMessage(Base):
    """Out-of-band sidecar for human free-text chat, OFF the hash chain (US-123).

    Human chat is personally-identifiable (PII) and must be erasable for GDPR.
    Putting the raw text inside a hash-chained ``game_events`` row would make
    erasure mathematically impossible without breaking deterministic replay, so
    the raw text lives ONLY here. The paired ``PublicMessageSubmitted`` /
    ``PrivateMessageSubmitted`` core event carries only an opaque ``content_ref``
    (a sha256), so redacting a sidecar row never changes any ``event_hash``.

    ``sequence`` pairs with the ``game_events.sequence`` of the message event, so
    a released/masked human message is reconstructable by joining on
    (``game_id``, ``sequence``). ``redact`` nulls ``raw_text``/``cleaned_text``
    and flips ``redacted`` without touching ``game_events``.
    """

    __tablename__ = "human_chat_messages"
    __table_args__ = (
        UniqueConstraint("game_id", "sequence", name="uq_human_chat_message_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(String, nullable=True)
    cleaned_text: Mapped[str | None] = mapped_column(String, nullable=True)
    redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Principal(Base):
    """A browser-human identity, completely separate from API-key auth (US-127).

    A principal is either a ``guest`` (created on first contact from an invite
    link, no signup) or an ``account`` (upgraded via OAuth in US-129). It carries
    no scope and is never reachable from the ``api_keys`` auth path — a guest
    cookie grants zero API scope and an API key grants zero human identity.
    ``deleted_at`` supports GDPR erasure without a hard delete.
    """

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class HumanSession(Base):
    """A browser session bound to a :class:`Principal` (US-127).

    The opaque session token is NEVER persisted: only its sha256 digest lives in
    ``session_hash`` and is compared with a constant-time comparison. ``kind``
    distinguishes a guest cookie from an account cookie. A session is invalid
    once ``revoked_at`` is set or ``expires_at`` is in the past.
    """

    __tablename__ = "human_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    session_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthIdentity(Base):
    """A provider account linked to an account :class:`Principal` (US-129).

    Keyed by (``provider``, ``subject``) so an OAuth sign-in is find-or-create:
    a repeat sign-in resolves the same account principal across sessions and
    devices. Only the stable provider ``subject`` is persisted — provider access
    tokens are NEVER stored beyond completing the exchange. There is no friends
    graph and no multi-account merge in v1.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_oauth_identity_provider_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class HumanConsent(Base):
    """An append-only record of a human accepting a legal document (US-130).

    A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm they
    are 16+ (``AGE_GATE``) before sending any action or chat. One combined tap
    records all three kinds at their current ``document_version``. Rows are NEVER
    updated or deleted in place — a fresh acceptance (e.g. after a document
    version bump that re-prompts) appends new rows, so consent history is a
    complete audit trail. ``source_ip_hash`` is an optional sha256 of the
    accepting client's IP (never the raw IP), supporting abuse review without
    storing PII.

    Consent is enforced in the api/runner shell, NEVER in the pure core: the
    first human action or chat submission is rejected unless a current consent
    for every required document kind exists for the principal.
    """

    __tablename__ = "human_consents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    document_kind: Mapped[str] = mapped_column(String, nullable=False)
    document_version: Mapped[str] = mapped_column(String, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    source_ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)


class HumanGameRuntime(Base):
    """Durable, rehydratable live scaffolding for an in-progress human game (US-131).

    A human-lane game can last minutes to hours, so a process restart must not
    lose it. This row holds ONLY the *impure* live runner scaffolding — the
    current ``phase``, the wall-clock ``deadline_at`` for that phase, and an
    opaque ``buffer_snapshot`` of in-flight per-seat human submissions awaiting
    release. It is keyed one-to-one by ``game_id``.

    The deterministic core game state is NEVER stored here: it is always
    reconstructed by replaying the hash-chained ``game_events`` log (hard rule
    4). The snapshot exists only so the impure shell (deadlines, buffered human
    input) survives a restart; if it disagreed with the event log, the event log
    wins. This uses the existing async DB — there is no Redis (stack rule).
    """

    __tablename__ = "human_game_runtime"

    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    phase: Mapped[str] = mapped_column(String, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    buffer_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class HumanSeatPresence(Base):
    """Per-seat human presence heartbeat for disconnect-grace takeover (US-162).

    This row is live transport metadata only. The game remains replayable from
    the hash-chained event log; presence only lets the impure human worker lane
    decide when a dropped human seat has exceeded the reconnect grace window and
    should be silently taken over by a curated AI.
    """

    __tablename__ = "human_seat_presence"
    __table_args__ = (
        UniqueConstraint("game_id", "public_player_id", name="uq_human_seat_presence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    connected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class HumanActionSubmission(Base):
    """An authenticated human's structured action for one seat/phase (US-134).

    A human submits a structured ``Action`` (``type`` + optional ``target``,
    exactly :class:`padrino.core.engine.actions.Action`) over an authenticated
    POST channel. The submission is validated server-side against
    ``legal_actions_for`` and stored here so the human-aware tick (US-137/138) can
    later resolve the seat's turn from buffered input.

    ``idempotency_key`` dedupes network retries: a row is unique per
    ``(game_id, public_player_id, phase, idempotency_key)``, so a retried POST
    with the same key returns the already-recorded action rather than
    double-voting. A later submission for the same seat+phase with a *different*
    key overwrites the seat's pending action (the human changed their mind),
    keyed off ``(game_id, public_player_id, phase)``.

    Raw chat text never lives here — this table holds only the mechanical action.
    """

    __tablename__ = "human_action_submissions"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "public_player_id",
            "phase",
            "idempotency_key",
            name="uq_human_action_idempotency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class HumanChatSubmission(Base):
    """An authenticated human's chat message in the buffered *hold* (US-135).

    A human submits a public/private chat message over an authenticated POST
    channel. The message enters this buffer hold and is gated by the
    block-before-release moderation hook (US-140 lands the verdict) before any
    release: a held message starts ``status='HELD'`` and is flipped to
    ``'RELEASED'`` only after moderation passes, or ``'BLOCKED'`` and never
    released. On release the raw text is routed to the out-of-band
    :class:`HumanChatMessage` sidecar (US-123) — it is NEVER inlined in a
    hash-chained event payload (the paired core event carries only an opaque
    ``content_ref``), so it stays GDPR-redactable without breaking the chain.

    ``idempotency_key`` dedupes network retries: a row is unique per
    ``(game_id, public_player_id, phase, idempotency_key)``, so a retried POST
    with the same key returns the already-held/released message rather than
    inserting a duplicate. The chat firewall holds: this text drives no
    mechanics — only a structured ``Action`` (US-134) mutates state.
    """

    __tablename__ = "human_chat_submissions"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "public_player_id",
            "phase",
            "idempotency_key",
            name="uq_human_chat_idempotency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    public_player_id: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    raw_text: Mapped[str] = mapped_column(String, nullable=False)
    cleaned_text: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="HELD", server_default="HELD"
    )
    sidecar_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HumanTuringGuess(Base):
    """One human's post-terminal spot-the-AI guess + personal score (US-144).

    After a human game terminates, each human submits ONE guess assigning
    HUMAN/AI to every OTHER seat over the existing human channel (a thin
    post-terminal step, not a new FSM phase). The pure
    :func:`padrino.core.turing.scoring.score_guess` computes the guesser's
    detection accuracy; the guess (``guess`` JSON: ``{public_player_id: label}``)
    and the result (``total`` / ``correct`` / ``accuracy``) persist here.

    Exactly one guess per ``(game_id, guesser_public_id)`` (a guesser guesses
    once); a retry returns the stored row rather than re-scoring. There is NO
    leaderboard - this row holds one guesser's personal stat only.
    """

    __tablename__ = "human_turing_guesses"
    __table_args__ = (
        UniqueConstraint("game_id", "guesser_public_id", name="uq_human_turing_guess"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    guesser_public_id: Mapped[str] = mapped_column(String, nullable=False)
    guess: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    accuracy: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Lobby(Base):
    """A private friend lobby configuring one human-multiplayer game (US-147).

    A host creates a lobby and configures the game it will launch: the
    ``ruleset_id`` (``mini7_v1`` / ``bench10_v1``), the per-game ``identity_mode``
    (default ``ANONYMOUS``), a static ``theme_pack_id`` from the sprite library
    (US-152), and ``stakes`` pinned to ``CASUAL`` (decision 10 — ELO infra is
    dormant in v1). ``status`` walks ``OPEN -> LOCKED -> LAUNCHED`` (or ``CLOSED``
    on cancel). ``lobby_seed`` is the deterministic seed the curated auto-fill
    (US-149) consumes so seat assignment is reproducible. ``host_principal_id``
    is the human who created it; ``league_id`` is the dormant Humans-Included
    league (segregated from the scientific benchmark, hard rule 8). ``game_id``
    is null until launch handoff materializes the real game.

    There is NO public matchmaking in v1: lobbies are private friend lobbies
    reachable only via an invite link (US-148).
    """

    __tablename__ = "lobbies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ruleset_id: Mapped[str] = mapped_column(String, nullable=False)
    identity_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="ANONYMOUS", server_default="ANONYMOUS"
    )
    #: Opaque single-use-per-person invite token (US-148). A friend joins via
    #: ``POST /lobbies/join/{invite_token}``; membership is what makes a join
    #: single-use-per-person (re-joining is idempotent), so the token itself is a
    #: shareable, reusable address for the lobby, not a one-time code.
    invite_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    theme_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stakes: Mapped[str] = mapped_column(
        String, nullable=False, default="CASUAL", server_default="CASUAL"
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="OPEN", server_default="OPEN"
    )
    lobby_seed: Mapped[str] = mapped_column(String, nullable=False)
    host_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    game_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("games.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class LobbyMember(Base):
    """A human (host or invited friend) who is a member of a lobby (US-147).

    A member links a :class:`Lobby` to the human :class:`Principal` who joined it;
    ``is_host`` marks the creator. Membership is unique per
    ``(lobby_id, principal_id)`` so a person joins a lobby once (US-148 enforces
    single-use-per-person joins, ready-up, presence). The pre-seat roster lives
    here; the concrete seat layout the game launches with lives in
    :class:`LobbySeat`.
    """

    __tablename__ = "lobby_members"
    __table_args__ = (UniqueConstraint("lobby_id", "principal_id", name="uq_lobby_member"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lobby_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("lobbies.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    is_host: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LobbySeat(Base):
    """One configured seat in a lobby's pre-launch seat layout (US-147).

    Each seat has a fixed ``seat_index`` in the game-to-be. ``seat_kind``
    (:class:`padrino.core.enums.LobbySeatKind`) marks whether the seat is reserved
    for a HUMAN member or will be filled by an AI. A HUMAN seat may reference the
    :class:`LobbyMember` reserving it (``member_id``); an AI seat may pin the
    host's pre-picked human-eligible model (``agent_build_id``) or be left null for
    curated deterministic auto-fill at launch (US-149). Seats are unique per
    ``(lobby_id, seat_index)``.

    This holds counts-only-safe configuration data; the canonical disclosed
    composition still flows through :func:`padrino.core.composition.composition_summary`
    so no per-seat human/AI map ever leaks (US-126/US-142).
    """

    __tablename__ = "lobby_seats"
    __table_args__ = (UniqueConstraint("lobby_id", "seat_index", name="uq_lobby_seat_index"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lobby_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("lobbies.id", ondelete="CASCADE"), nullable=False
    )
    seat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    seat_kind: Mapped[str] = mapped_column(String, nullable=False)
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("lobby_members.id", ondelete="SET NULL"), nullable=True
    )
    agent_build_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=True
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
    "HumanActionSubmission",
    "HumanChatMessage",
    "HumanChatSubmission",
    "HumanConsent",
    "HumanCostAdmission",
    "HumanGameRuntime",
    "HumanRating",
    "HumanRatingEvent",
    "HumanSeatPresence",
    "HumanSession",
    "IngestedGame",
    "JudgeEnrichmentCard",
    "League",
    "LlmCall",
    "Lobby",
    "LobbyMember",
    "LobbySeat",
    "MaterializedGameAnalytics",
    "ModelConfig",
    "ModelProvider",
    "OAuthIdentity",
    "Principal",
    "PromptVersion",
    "RateLimitBucket",
    "Rating",
    "RatingEvent",
    "ScheduledGauntlet",
    "SchedulerHeartbeat",
]
