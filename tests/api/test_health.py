"""Tests for /healthz and /readyz endpoints (US-041)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from padrino.api.app import create_app
from padrino.db.base import Base, create_engine, create_session_factory


async def _http_client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    return AsyncClient(transport=transport, base_url="http://testserver")


async def test_healthz_returns_ok() -> None:
    app = create_app()
    client = await _http_client(app)
    async with client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_when_db_reachable() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        app = create_app(session_factory=factory)
        client = await _http_client(app)
        async with client:
            response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
    finally:
        await engine.dispose()


async def test_readyz_503_when_db_unreachable() -> None:
    engine = create_engine(
        "sqlite+aiosqlite:////definitely/nonexistent/path/padrino_unreachable.db"
    )
    factory = create_session_factory(engine)
    try:
        app = create_app(session_factory=factory)
        client = await _http_client(app)
        async with client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "error"
        assert body["database"] == "error"
        assert "detail" in body
    finally:
        await engine.dispose()


async def test_readyz_503_when_no_session_factory_configured() -> None:
    app = create_app()  # no session_factory wired
    client = await _http_client(app)
    async with client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["database"] == "unconfigured"


def test_create_app_returns_fastapi_instance() -> None:
    from fastapi import FastAPI

    app = create_app()
    assert isinstance(app, FastAPI)


def test_cli_has_serve_subcommand() -> None:
    from typer.testing import CliRunner

    from padrino.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout.lower() or "host" in result.stdout.lower()


def test_serve_command_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from padrino import cli

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("padrino.cli.uvicorn.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["serve", "--host", "127.0.0.1", "--port", "9999"])
    assert result.exit_code == 0, result.stdout
    assert captured["app"] is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("host") == "127.0.0.1"
    assert kwargs.get("port") == 9999


async def test_cors_disabled_by_default() -> None:
    app = create_app()
    client = await _http_client(app)
    async with client:
        response = await client.get(
            "/healthz",
            headers={"Origin": "http://dashboard.example"},
        )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


async def test_cors_enabled_when_origins_passed() -> None:
    app = create_app(cors_allow_origins=["http://dashboard.example"])
    client = await _http_client(app)
    async with client:
        response = await client.get(
            "/healthz",
            headers={"Origin": "http://dashboard.example"},
        )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://dashboard.example"


async def test_cors_preflight_allows_dashboard_json_mutations() -> None:
    app = create_app(cors_allow_origins=["http://dashboard.example"])
    client = await _http_client(app)
    async with client:
        response = await client.options(
            "/human/games/00000000-0000-0000-0000-000000000000/turing-guess",
            headers={
                "Origin": "http://dashboard.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://dashboard.example"
    assert "POST" in response.headers.get("access-control-allow-methods", "")


async def test_cors_enabled_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from padrino import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("PADRINO_CORS_ALLOW_ORIGINS", "http://a.example, http://b.example")
    try:
        app = create_app()
        client = await _http_client(app)
        async with client:
            response = await client.get(
                "/healthz",
                headers={"Origin": "http://b.example"},
            )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://b.example"
    finally:
        settings_mod.get_settings.cache_clear()


async def test_readyz_handles_factory_exception() -> None:
    class BrokenFactory:
        def __call__(self) -> object:
            raise RuntimeError("boom")

    app = create_app(session_factory=BrokenFactory())  # type: ignore[arg-type]
    client = await _http_client(app)
    async with client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
