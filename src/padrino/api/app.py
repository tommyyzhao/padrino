"""FastAPI application factory for Padrino.

Exposes :func:`create_app` returning a configured :class:`fastapi.FastAPI`
instance with the always-on infrastructure routes (``/healthz``, ``/readyz``,
``/metrics``). Routes added in later stories (admin CRUD, gauntlets, game
inspection, leaderboard) attach to the same app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.auth import RateLimiter, admin_token_deprecation_middleware, require_read
from padrino.api.routes.admin import router as admin_router
from padrino.api.routes.admin_keys import router as admin_keys_router
from padrino.api.routes.games import router as games_router
from padrino.api.routes.gauntlets import router as gauntlets_router
from padrino.api.routes.health import router as health_router
from padrino.api.routes.leagues import router as leagues_router
from padrino.observability.metrics import (
    CONTENT_TYPE_LATEST,
    api_requests_total,
    render_prometheus_text,
)
from padrino.settings import get_settings

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


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    admin_token: str | None | Any = _UNSET,
    auth_required: bool = False,
    rate_limiter: RateLimiter | None = None,
    metrics_require_auth: bool | None = None,
) -> FastAPI:
    """Build and return the Padrino FastAPI application.

    ``session_factory`` is optional. When omitted the app constructs a
    factory from :func:`padrino.settings.get_settings` lazily on the first
    readiness probe. Tests typically pass an explicit factory so the app
    does not touch the on-disk database.

    ``admin_token`` overrides ``Settings.padrino_admin_token`` for the
    legacy ``X-Padrino-Admin-Token`` back-compat shim (US-056). Defaults
    (when omitted) to the value from :func:`padrino.settings.get_settings`.

    ``auth_required`` (US-056) gates whether requests without a valid Bearer
    token are rejected with 401. Existing dev deployments and the legacy
    test suites run with ``False`` (the default) so unauthenticated
    requests synthesize an admin context. New deployments flip the bit on.

    ``rate_limiter`` injects a custom :class:`padrino.api.auth.RateLimiter`
    so tests can pin the clock without sleeping.

    ``metrics_require_auth`` (US-059) gates the ``GET /metrics`` endpoint
    behind the spectator scope. Defaults to
    :attr:`Settings.padrino_metrics_require_auth` (off — Prometheus scrape
    pattern). Flipping it on enforces the same scope check as the rest of
    the read surface.
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
    app.state.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
    require_metrics_auth = (
        settings.padrino_metrics_require_auth
        if metrics_require_auth is None
        else metrics_require_auth
    )
    app.middleware("http")(admin_token_deprecation_middleware)
    app.middleware("http")(metrics_middleware)
    app.include_router(admin_router)
    app.include_router(admin_keys_router)
    app.include_router(leagues_router)
    app.include_router(gauntlets_router)
    app.include_router(games_router)
    app.include_router(health_router)

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
