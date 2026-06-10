"""Padrino application settings loaded from environment variables."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from padrino.llm.adapter import RoutingPolicy


class Settings(BaseSettings):
    """Typed configuration for Padrino, loaded from .env and environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider credentials (optional so the engine works without real keys)
    cerebras_api_key: str | None = None
    deepinfra_api_key: str | None = None
    zai_api_key: str | None = None
    xiaomi_api_key: str | None = None

    # Database
    padrino_db_url: str = "sqlite+aiosqlite:///./padrino.db"
    # Connection-pool tuning is wired to ``create_async_engine`` only when the
    # configured URL targets Postgres (asyncpg). SQLite uses aiosqlite which
    # doesn't honor a server-side pool, so these knobs are no-ops there.
    padrino_db_pool_size: int = 5
    padrino_db_pool_max_overflow: int = 10

    # Logging
    padrino_log_level: str = "INFO"

    # LLM inference
    padrino_llm_timeout_seconds: int = 45
    padrino_temperature: float = 0.7
    padrino_top_p: float = 1.0

    # Model routing
    padrino_primary_model: str = "cerebras/zai-glm-4.7"
    padrino_fallback_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash"

    # Same-model multi-host fallback (US-079). When true AND ``ZAI_API_KEY``
    # resolves, ``build_routing_policy`` injects Z.AI's GLM-4.7 endpoint as
    # an alternate host for any Cerebras GLM-4.7 primary. The name reads like
    # a sentence: "Cerebras zai-glm-4.7 falls back to Z.AI". Disabling this
    # collapses behaviour to the wave-3 single-primary-plus-different-fallback
    # routing.
    padrino_cerebras_zai_glm47_zai_fallback: bool = True

    # Z.AI exposes two OpenAI-compatible endpoints:
    #  - the General API  ``https://api.z.ai/api/paas/v4``
    #  - the Coding-Plan API ``https://api.z.ai/api/coding/paas/v4`` (only
    #    callable while subscribed to the GLM Coding Plan).
    # The Coding Plan is what our paid subscription serves, so the default
    # points there. Override via ``PADRINO_ZAI_API_BASE`` if you're on the
    # General API.
    padrino_zai_api_base: str = "https://api.z.ai/api/coding/paas/v4"

    # Xiaomi token-plan serves OpenAI-compatible chat completions from a
    # custom base URL. ``LiteLlmAdapter`` forwards this to LiteLLM as
    # ``api_base`` while resolving credentials from ``XIAOMI_API_KEY``.
    xiaomi_base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1"

    # API
    padrino_admin_token: str | None = None

    # Worker count of the deployed uvicorn process (US-074). When > 1 the
    # API factory auto-selects the Postgres-backed shared rate-limit store
    # so per-key ceilings stay accurate across replicas. SQLite deployments
    # always stick with the in-memory store regardless of this value
    # because a single SQLite file is single-writer anyway.
    padrino_api_workers: int = 1

    # API-key auth (US-056). When the app is built with ``auth_required=True``
    # every request must carry a valid Bearer token (or the back-compat
    # ``X-Padrino-Admin-Token`` header). Rate limits are per-key sliding
    # windows expressed in requests per minute; the per-scope defaults below
    # match the priorities of each role (admin > spectator > submitter).
    padrino_rate_limit_admin_per_minute: int = 600
    # The submitter ceiling defaults to 30/min — set conservatively because
    # ``POST /ingest/game`` (US-062) is the only submitter-scoped endpoint and
    # one submission carries up to 10 MB of replayed event history.
    padrino_rate_limit_submitter_per_minute: int = 30
    padrino_rate_limit_spectator_per_minute: int = 1200
    padrino_rate_limit_anonymous_per_minute: int = 60

    # Prometheus metrics (US-059). The default exposes ``GET /metrics`` to any
    # scraper that can reach the process; flipping the flag requires the same
    # spectator scope as the read-only API surface.
    padrino_metrics_require_auth: bool = False

    # Public read API (US-063). The ``/public/*`` routes default to requiring
    # the spectator scope (or admin). Flipping this on serves the federated
    # leaderboard + ingested-bundle reads anonymously, which is the right
    # default for a centrally-hosted shared leaderboard.
    padrino_public_leaderboard_anonymous: bool = False

    # CORS (US-070). Comma-separated list of allowed origins for the SvelteKit
    # dashboard (and any other browser-side consumer). Empty string disables
    # CORS entirely — the API responds without ``Access-Control-Allow-*``
    # headers, matching the wave-1 default. ``*`` is supported and disables
    # the credentialed-origin echo (matches Starlette's CORSMiddleware
    # semantics).
    padrino_cors_allow_origins: str = ""

    # Post-game behavioral evaluation pipeline (Wave 6)
    padrino_enable_behavioral_evaluation: bool = False
    padrino_behavioral_judge_model: str = "xiaomi/mimo-v2.5-pro"

    # Moderation gate (US-093). Guard model is a DeepInfra-hosted Llama-Guard
    # family model routed through the existing LiteLLM adapter with
    # DEEPINFRA_API_KEY — no new credential required.
    padrino_guard_model: str = "deepinfra/meta-llama/Llama-Guard-3-8B"

    # Global spend governor (US-095). Hard ceiling on cumulative AI spend
    # across all house-funded games.  $200 is the operator-approved budget;
    # override via PADRINO_GLOBAL_SPEND_CAP_USD.
    padrino_global_spend_cap_usd: float = 200.0

    # Broadcast cadence (US-088). Delays (ms) applied by the SSE transport layer
    # between consecutive public_event_v1 frames.  Tune without a code change.
    padrino_broadcast_cadence_chat_ms: int = 2500
    padrino_broadcast_cadence_phase_ms: int = 3000
    padrino_broadcast_cadence_elimination_ms: int = 4000
    padrino_broadcast_cadence_resolution_ms: int = 3500
    padrino_broadcast_cadence_default_ms: int = 1500

    def build_routing_policy(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
    ) -> RoutingPolicy:
        """Compose a :class:`RoutingPolicy` honoring the same-model fallback flags.

        Defaults to ``padrino_primary_model`` / ``padrino_fallback_model``.
        When ``padrino_cerebras_zai_glm47_zai_fallback`` is enabled, the primary
        is ``cerebras/zai-glm-4.7``, and ``zai_api_key`` is set, the returned
        policy carries a single :class:`SameModelHost` pointing at Z.AI's
        ``openai/glm-4.7`` endpoint. When any of those conditions is false the
        returned policy's ``same_model_hosts`` tuple is empty — behaviour is
        identical to the wave-3 routing.
        """
        # Local import keeps ``settings`` out of the import graph rooted at
        # ``padrino.llm.adapter`` callers that don't need it.
        from padrino.llm.adapter import RoutingPolicy, SameModelHost

        primary = primary_model if primary_model is not None else self.padrino_primary_model
        fallback = fallback_model if fallback_model is not None else self.padrino_fallback_model

        same_model_hosts: tuple[SameModelHost, ...] = ()
        if (
            self.padrino_cerebras_zai_glm47_zai_fallback
            and self.zai_api_key
            and primary == "cerebras/zai-glm-4.7"
        ):
            same_model_hosts = (
                SameModelHost(
                    provider="zai",
                    litellm_model_id="openai/glm-4.7",
                    api_base=self.padrino_zai_api_base,
                    auth_secret_ref="env:ZAI_API_KEY",
                ),
            )
        return RoutingPolicy(
            primary_model=primary,
            fallback_model=fallback,
            same_model_hosts=same_model_hosts,
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
