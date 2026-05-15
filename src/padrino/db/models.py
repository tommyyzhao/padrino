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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String, nullable=True)
    event_hash_head: Mapped[str | None] = mapped_column(String, nullable=True)


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
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    faction: Mapped[str] = mapped_column(String, nullable=False)
    alive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    death_phase: Mapped[str | None] = mapped_column(String, nullable=True)


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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RatingEvent(Base):
    __tablename__ = "rating_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    league_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("leagues.id"), nullable=False)
    game_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("games.id"), nullable=False)
    agent_build_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_builds.id"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_value: Mapped[str] = mapped_column(String, nullable=False)
    before_mu: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    before_sigma: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    after_mu: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    after_sigma: Mapped[float] = mapped_column(Numeric(asdecimal=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "AgentBuild",
    "Game",
    "GameEvent",
    "GameSeat",
    "Gauntlet",
    "GauntletRosterSlot",
    "League",
    "LlmCall",
    "ModelConfig",
    "ModelProvider",
    "PromptVersion",
    "Rating",
    "RatingEvent",
]
