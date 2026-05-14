"""SQLAlchemy 2.x ORM models for Padrino's core schema (prd.md §12).

This module covers the nine tables required for build/league/gauntlet/game
metadata: ``model_providers``, ``model_configs``, ``prompt_versions``,
``agent_builds``, ``leagues``, ``gauntlets``, ``gauntlet_roster_slots``,
``games``, ``game_seats``. The append-only ``game_events`` table, the
``llm_calls`` table, and the ratings tables are intentionally deferred
to later stories.
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
    terminal_result: Mapped[str | None] = mapped_column(String, nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String, nullable=True)
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


__all__ = [
    "AgentBuild",
    "Game",
    "GameSeat",
    "Gauntlet",
    "GauntletRosterSlot",
    "League",
    "ModelConfig",
    "ModelProvider",
    "PromptVersion",
]
