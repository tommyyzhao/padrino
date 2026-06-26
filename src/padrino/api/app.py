"""FastAPI application factory for Padrino.

Exposes :func:`create_app` returning a configured :class:`fastapi.FastAPI`
instance with the always-on infrastructure routes (``/healthz``, ``/readyz``,
``/metrics``). Routes added in later stories (admin CRUD, gauntlets, game
inspection, leaderboard) attach to the same app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.auth import RateLimiter, admin_token_deprecation_middleware, require_read
from padrino.api.rate_limit_store import (
    DatabaseRateLimitStore,
    InMemoryRateLimitStore,
    RateLimitStore,
)
from padrino.api.routes.admin import router as admin_router
from padrino.api.routes.admin_keys import router as admin_keys_router
from padrino.api.routes.games import router as games_router
from padrino.api.routes.gauntlets import router as gauntlets_router
from padrino.api.routes.health import router as health_router
from padrino.api.routes.human import router as human_router
from padrino.api.routes.ingest import router as ingest_router
from padrino.api.routes.leagues import router as leagues_router
from padrino.api.routes.lobbies import router as lobbies_router
from padrino.api.routes.public import router as public_router
from padrino.api.routes.scheduled_gauntlets import router as scheduled_gauntlets_router
from padrino.api.routes.sprites import router as sprites_router
from padrino.observability.metrics import (
    CONTENT_TYPE_LATEST,
    api_requests_total,
    render_prometheus_text,
)
from padrino.settings import Settings, get_settings

_UNSET: Any = object()


def _route_template(request: Request) -> str:
    """Return the route template (``/games/{id}``) or the raw path as fallback."""
    route: Any = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


async def metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    """Increment ``padrino_api_requests_total`` for every HTTP response served."""
    response = await call_next(request)
    api_requests_total.labels(
        route=_route_template(request),
        method=request.method,
        status=str(response.status_code),
    ).inc()
    return response


def _select_rate_limit_store(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
    settings: Settings,
) -> RateLimitStore:
    """Auto-select the per-key rate-limit store backing.

    Multi-worker Postgres deployments need a shared counter so per-key
    ceilings stay accurate across replicas; everything else (single-worker,
    SQLite, tests) keeps the in-process default.
    """
    if (
        session_factory is not None
        and settings.padrino_api_workers > 1
        and settings.padrino_db_url.startswith("postgresql")
    ):
        return DatabaseRateLimitStore(session_factory=session_factory)
    return InMemoryRateLimitStore()


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    admin_token: str | None | Any = _UNSET,
    auth_required: bool = True,
    rate_limiter: RateLimiter | None = None,
    rate_limit_store: RateLimitStore | None = None,
    metrics_require_auth: bool | None = None,
    cors_allow_origins: Sequence[str] | None = None,
    public_surface_only: bool | None = None,
) -> FastAPI:
    """Build and return the Padrino FastAPI application.

    ``session_factory`` is optional. When omitted the app constructs a
    factory from :func:`padrino.settings.get_settings` lazily on the first
    readiness probe. Tests typically pass an explicit factory so the app
    does not touch the on-disk database.

    ``admin_token`` overrides ``Settings.padrino_admin_token`` for the
    legacy ``X-Padrino-Admin-Token`` back-compat shim (US-056). Defaults
    (when omitted) to the value from :func:`padrino.settings.get_settings`.

    ``auth_required`` (US-056, US-074) gates whether requests without a
    valid Bearer token are rejected with 401. ``True`` is the default as
    of US-074; tests and unauthenticated dev environments opt out by
    passing ``False`` (in which case requests synthesize an admin
    context).

    ``rate_limiter`` injects a custom :class:`padrino.api.auth.RateLimiter`
    so tests can pin the clock without sleeping.

    ``rate_limit_store`` (US-074) injects a custom backing store for the
    per-key counter; when omitted the factory auto-selects
    :class:`DatabaseRateLimitStore` for multi-worker Postgres deployments
    (``Settings.padrino_api_workers > 1`` and the DB URL is Postgres) and
    :class:`InMemoryRateLimitStore` everywhere else.

    ``metrics_require_auth`` (US-059) gates the ``GET /metrics`` endpoint
    behind the spectator scope. Defaults to
    :attr:`Settings.padrino_metrics_require_auth` (off — Prometheus scrape
    pattern). Flipping it on enforces the same scope check as the rest of
    the read surface.

    ``cors_allow_origins`` (US-070) wires Starlette's ``CORSMiddleware``
    when the sequence is non-empty. Defaults to the comma-separated list
    parsed from :attr:`Settings.padrino_cors_allow_origins`; empty leaves
    CORS off and the API behaves as wave-1.

    ``public_surface_only`` (US-110) restricts the app to ONLY the public
    spectator router plus the health probes (``/healthz``, ``/readyz``).
    When on, the private routers (admin, admin_keys, ingest, games,
    leagues, gauntlets, scheduled_gauntlets) and ``/metrics`` are not
    registered at all — a request to any private prefix 404s rather than
    401/403, because the route never exists in this process. Defaults to
    :attr:`Settings.padrino_public_surface_only` (off). This is the
    architectural embodiment of "website public, everything else private":
    even a reverse-proxy misconfiguration cannot leak a private route.
    """
    settings = get_settings()
    app = FastAPI(
        title="Padrino",
        description="Deterministic LLM benchmark and league engine for Mafia-style social deduction.",
        version="0.1.0",
    )
    app.state.session_factory = session_factory
    if admin_token is _UNSET:
        app.state.admin_token = settings.padrino_admin_token
    else:
        app.state.admin_token = admin_token
    app.state.auth_required = auth_required
    app.state.auth_settings = settings
    if rate_limiter is not None:
        app.state.rate_limiter = rate_limiter
    else:
        store = (
            rate_limit_store
            if rate_limit_store is not None
            else _select_rate_limit_store(
                session_factory=session_factory,
                settings=settings,
            )
        )
        app.state.rate_limiter = RateLimiter(store=store)
    require_metrics_auth = (
        settings.padrino_metrics_require_auth
        if metrics_require_auth is None
        else metrics_require_auth
    )
    surface_only = (
        settings.padrino_public_surface_only if public_surface_only is None else public_surface_only
    )
    if cors_allow_origins is None:
        origins = [o.strip() for o in settings.padrino_cors_allow_origins.split(",") if o.strip()]
    else:
        origins = [o for o in cors_allow_origins if o]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=("*" not in origins),
            allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type", "Accept"],
        )
    app.middleware("http")(admin_token_deprecation_middleware)
    app.middleware("http")(metrics_middleware)
    if not surface_only:
        app.include_router(admin_router)
        app.include_router(admin_keys_router)
        app.include_router(leagues_router)
        app.include_router(gauntlets_router)
        app.include_router(games_router)
        app.include_router(ingest_router)
        app.include_router(scheduled_gauntlets_router)
    app.include_router(health_router)
    app.include_router(public_router)
    # Human quickplay (US-128) is always mounted: it carries no API-scope
    # dependency and must be reachable even under auth_required=True and the
    # public-surface-only deployment (humans play from the public site).
    app.include_router(human_router)
    # Private friend lobbies (US-147) are human-identity-scoped (no API scope),
    # so they mount alongside the human router and are reachable under
    # auth_required=True and the public-surface-only deployment.
    app.include_router(lobbies_router)
    # Static themed sprite library (US-152): read-only, identity-blind art served
    # with an immutable cache. Always mounted (reachable on the public surface).
    app.include_router(sprites_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        factory: Any = app.state.session_factory
        if factory is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "database": "unconfigured",
                    "detail": "no session_factory configured",
                },
            )
        try:
            async with factory() as session:
                result = await session.execute(text("SELECT 1"))
                value = result.scalar_one()
        except Exception as exc:  # pragma: no cover - error message varies
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "database": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )
        if value != 1:  # pragma: no cover - defensive
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "database": "error",
                    "detail": f"SELECT 1 returned {value!r}",
                },
            )
        return JSONResponse(status_code=200, content={"status": "ok", "database": "ok"})

    if not surface_only:
        if require_metrics_auth:

            @app.get("/metrics", dependencies=[Depends(require_read)])
            def metrics_auth() -> Response:
                return PlainTextResponse(
                    content=render_prometheus_text(),
                    media_type=CONTENT_TYPE_LATEST,
                )

        else:

            @app.get("/metrics")
            def metrics_open() -> Response:
                return PlainTextResponse(
                    content=render_prometheus_text(),
                    media_type=CONTENT_TYPE_LATEST,
                )

    return app


__all__ = ["create_app", "metrics_middleware"]
