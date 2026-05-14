"""FastAPI application factory for Padrino.

Exposes :func:`create_app` returning a configured :class:`fastapi.FastAPI`
instance with the always-on infrastructure routes (``/healthz`` and
``/readyz``). Routes added in later stories (admin CRUD, gauntlets, game
inspection, leaderboard) attach to the same app.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.routes.admin import router as admin_router
from padrino.api.routes.gauntlets import router as gauntlets_router
from padrino.api.routes.leagues import router as leagues_router
from padrino.db.base import create_engine, create_session_factory
from padrino.settings import get_settings


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    settings = get_settings()
    engine = create_engine(settings.padrino_db_url)
    return create_session_factory(engine)


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Build and return the Padrino FastAPI application.

    ``session_factory`` is optional. When omitted the app constructs a
    factory from :func:`padrino.settings.get_settings` lazily on the first
    readiness probe. Tests typically pass an explicit factory so the app
    does not touch the on-disk database.
    """
    app = FastAPI(
        title="Padrino",
        description="Deterministic LLM benchmark and league engine for Mafia-style social deduction.",
        version="0.1.0",
    )
    app.state.session_factory = session_factory
    app.include_router(admin_router)
    app.include_router(leagues_router)
    app.include_router(gauntlets_router)

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

    return app


__all__ = ["create_app"]
