"""Declarative base and async session factory for Padrino's persistence layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base class shared by every Padrino ORM model."""


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine(
    url: str,
    *,
    echo: bool = False,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Build an async engine for Padrino's supported dialects.

    The dialect is selected from the URL scheme: ``sqlite+aiosqlite://`` for
    local single-writer deployments (FK enforcement is enabled via a connect
    listener since SQLite leaves it off by default) and
    ``postgresql+asyncpg://`` for shared / managed deployments (FKs are on by
    default; only Postgres receives the optional ``pool_size`` /
    ``max_overflow`` kwargs because the SQLite ``StaticPool`` and aiosqlite
    connection model don't honor a server-side connection pool).
    """
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.startswith("postgresql"):
        if pool_size is not None:
            kwargs["pool_size"] = pool_size
        if max_overflow is not None:
            kwargs["max_overflow"] = max_overflow
    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an async session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session and commit on success / rollback on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
