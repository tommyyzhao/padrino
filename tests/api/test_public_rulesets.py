"""Tests for the public built-in ruleset metadata endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.core.rulesets import BUILTIN_RULESET_IDS
from padrino.settings import Settings, get_settings


def _anon_settings() -> Settings:
    return Settings(
        padrino_public_leaderboard_anonymous=True,
        padrino_rate_limit_anonymous_per_minute=10_000,
    )


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    app.state.auth_settings = _anon_settings()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_public_rulesets_returns_builtin_ruleset_metadata(client: AsyncClient) -> None:
    response = await client.get("/public/rulesets")
    assert response.status_code == 200, response.text

    body = response.json()
    items = body["items"]
    returned_ids = [item["ruleset_id"] for item in items]
    assert returned_ids == list(BUILTIN_RULESET_IDS)

    by_id = {item["ruleset_id"]: item for item in items}
    for ruleset_id in (
        "mini7_v1",
        "bench10_v1",
        "deception13_v1",
        "roleblock10_v1",
        "visit12_v1",
        "ninja13_v1",
        "sk12_v1",
        "jester8_v1",
    ):
        assert ruleset_id in by_id
        assert by_id[ruleset_id]["player_count"] > 0
        assert by_id[ruleset_id]["label"]
        assert by_id[ruleset_id]["rating_context_kind"]
        assert isinstance(by_id[ruleset_id]["is_canonical"], bool)
