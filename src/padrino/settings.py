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
    # Browser-human principal rate limit (US-127). Per-human-principal sliding
    # window, completely separate from the API-key ceilings above. Reuses the
    # same RateLimitStore, keyed by the session hash so it shares no bucket with
    # any api_key.
    padrino_rate_limit_human_per_minute: int = 120

    # Guest quickplay (US-128). How long a freshly-minted guest/account human
    # session cookie stays valid, and whether the cookie carries the ``Secure``
    # attribute. ``Secure`` defaults on (HTTPS-only) for production; tests and
    # local plain-HTTP dev flip it off so the cookie survives an http:// client.
    padrino_human_session_ttl_hours: int = 720
    padrino_human_session_cookie_secure: bool = True

    # Optional OAuth sign-in, ONE provider (US-129). All fields default to None
    # so the engine boots and the test suite runs WITHOUT any provider
    # credentials (the actual OAuth app is a deploy-time human step). When the
    # client id/secret + endpoint urls are all present the ``/human/oauth/*``
    # routes are live; otherwise they 503. The client secret is never logged.
    padrino_oauth_provider: str | None = None
    padrino_oauth_client_id: str | None = None
    padrino_oauth_client_secret: str | None = None
    padrino_oauth_authorize_url: str | None = None
    padrino_oauth_token_url: str | None = None
    padrino_oauth_userinfo_url: str | None = None
    padrino_oauth_redirect_url: str | None = None
    padrino_oauth_scope: str = "openid email profile"

    # Prometheus metrics (US-059). The default exposes ``GET /metrics`` to any
    # scraper that can reach the process; flipping the flag requires the same
    # spectator scope as the read-only API surface.
    padrino_metrics_require_auth: bool = False

    # Public read API (US-063). The ``/public/*`` routes default to requiring
    # the spectator scope (or admin). Flipping this on serves the federated
    # leaderboard + ingested-bundle reads anonymously, which is the right
    # default for a centrally-hosted shared leaderboard.
    padrino_public_leaderboard_anonymous: bool = False

    # Public-surface-only API mode (US-110). When True, ``create_app`` mounts
    # ONLY the public spectator router and the health probes; every private
    # router (admin, admin_keys, ingest, games, leagues, gauntlets,
    # scheduled_gauntlets) and ``/metrics`` are not registered at all. The
    # internet-facing process therefore cannot leak a private route even if a
    # reverse proxy is misconfigured — defense in depth. ``/metrics`` is
    # scraped against the internal (full-surface) instance instead.
    padrino_public_surface_only: bool = False

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

    # Provisional/decay (US-099). An agent is provisional until it has played
    # at least padrino_provisional_game_threshold rated games. Sigma inflation
    # kicks in when a rated agent has been idle for more than
    # padrino_rating_decay_idle_days days; each additional idle day inflates
    # sigma by padrino_rating_decay_sigma_per_day (fraction, e.g. 0.05 = 5%).
    padrino_provisional_game_threshold: int = 10
    padrino_rating_decay_sigma_per_day: float = 0.05
    padrino_rating_decay_idle_days: int = 30

    # Continuous matchmaking (US-098). When True the scheduler tick also runs
    # the matchmaker → game runner → moderation gate pipeline each iteration.
    # Defaults to False so it must be explicitly opted in by the operator.
    padrino_enable_continuous_matchmaking: bool = False

    # Moderation gate (US-093). Guard model is a DeepInfra-hosted Llama-Guard
    # family model routed through the existing LiteLLM adapter with
    # DEEPINFRA_API_KEY — no new credential required.
    padrino_guard_model: str = "deepinfra/meta-llama/Llama-Guard-3-8B"

    # Global spend governor (US-095). Hard ceiling on cumulative AI spend
    # across all house-funded games.  $200 is the operator-approved budget;
    # override via PADRINO_GLOBAL_SPEND_CAP_USD.
    padrino_global_spend_cap_usd: float = 200.0

    # Admission / queue policy (US-096). Daily and concurrency caps that bound
    # how many games run independently of spend.  Defaults are conservative:
    # 20 games/day and 3 concurrent keeps cost predictable during initial rollout.
    padrino_max_games_per_day: int = 20
    padrino_max_concurrent_games: int = 3

    # Judge sampling enrichment (US-105). ``padrino_judge_sample_rate`` controls
    # the fraction of unevaluated completed games selected per batch run.
    # ``padrino_judge_max_games_per_run`` is a hard per-invocation ceiling that
    # acts as the per-run cost cap (one game = one judge LLM call).
    padrino_judge_sample_rate: float = 0.1
    padrino_judge_max_games_per_run: int = 5

    # Broadcast cadence (US-088). Delays (ms) applied by the SSE transport layer
    # between consecutive public_event_v1 frames.  Tune without a code change.
    padrino_broadcast_cadence_chat_ms: int = 2500
    padrino_broadcast_cadence_phase_ms: int = 3000
    padrino_broadcast_cadence_elimination_ms: int = 4000
    padrino_broadcast_cadence_resolution_ms: int = 3500
    padrino_broadcast_cadence_default_ms: int = 1500

    # SSE connection cap (US-107). Maximum concurrent SSE broadcast streams
    # per client IP. Excess connections are rejected with 429.
    padrino_sse_max_connections_per_ip: int = 5

    # Retention / archival (US-108). ``padrino_raw_payload_ttl_days`` controls
    # when heavy llm_call columns (request_json, raw_response) are scrubbed for
    # all completed games.  ``padrino_non_broadcastable_game_ttl_days`` controls
    # when non-broadcastable game rows (+ cascades) are hard-deleted.
    # Broadcastable games are never hard-deleted — ratings and replay data are
    # kept indefinitely.
    padrino_raw_payload_ttl_days: int = 30
    padrino_non_broadcastable_game_ttl_days: int = 7

    # Retention executor (US-116). The executor is wired into the scheduler tick
    # but stays inert unless ``padrino_enable_retention`` is True AND
    # ``padrino_retention_dry_run`` is explicitly flipped to False. With the
    # dry-run default on, the job only logs the plan and mutates nothing — both
    # flags must be set deliberately before any destructive action.
    padrino_enable_retention: bool = False
    padrino_retention_dry_run: bool = True

    # Operational alerting (US-113). ``padrino_alert_webhook_url`` is the
    # Slack/Discord/etc. incoming-webhook URL the human sets at deploy time;
    # when unset the notifier is log-only (no network call). The staleness
    # window decides when the scheduler heartbeat is considered dead, and the
    # streak threshold decides how many consecutive admission denials fire the
    # ``admission.denied.streak`` alert.
    padrino_alert_webhook_url: str | None = None
    padrino_alert_webhook_timeout_s: float = 5.0
    padrino_scheduler_heartbeat_stale_seconds: float = 120.0
    padrino_admission_denied_streak_threshold: int = 5

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
