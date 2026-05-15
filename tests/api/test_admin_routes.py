"""Tests for admin CRUD routes (US-042)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.db.base import Base, create_engine, create_session_factory


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub env vars referenced by ``auth_secret_ref`` so resolution succeeds.

    The admin POST /model-providers route resolves the secret reference at
    creation time (US-050). Tests use ``env:CEREBRAS_API_KEY`` and ``env:X``
    as placeholders — set both to test values for the duration of the test.
    """
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    monkeypatch.setenv("X", "test-x-value")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


_PROMPT_SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}}


async def _create_provider(client: AsyncClient, name: str = "cerebras") -> dict[str, object]:
    response = await client.post(
        "/model-providers",
        json={
            "name": name,
            "auth_secret_ref": "env:CEREBRAS_API_KEY",
            "base_url": "https://api.cerebras.ai/v1",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _create_model_config(client: AsyncClient, provider_id: str) -> dict[str, object]:
    response = await client.post(
        "/model-configs",
        json={
            "provider_id": provider_id,
            "model_name": "zai-glm-4.7",
            "default_temperature": 0.7,
            "default_top_p": 0.95,
            "default_max_output_tokens": 1024,
            "supports_structured_outputs": True,
            "model_version": "2026-04",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _create_prompt_version(
    client: AsyncClient, prompt_hash: str = "h" * 64
) -> dict[str, object]:
    response = await client.post(
        "/prompt-versions",
        json={
            "ruleset_id": "mini7_v1",
            "version": "v1",
            "system_prompt": "you are a mafia player",
            "developer_prompt": "respond with strict JSON",
            "response_schema": _PROMPT_SCHEMA,
            "prompt_hash": prompt_hash,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _create_agent_build(
    client: AsyncClient, model_config_id: str, prompt_version_id: str
) -> dict[str, object]:
    response = await client.post(
        "/agent-builds",
        json={
            "display_name": "glm-mini7",
            "model_config_id": model_config_id,
            "prompt_version_id": prompt_version_id,
            "adapter_version": "litellm/0.1",
            "inference_params": {"temperature": 0.5},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_create_model_provider(client: AsyncClient) -> None:
    provider = await _create_provider(client)
    assert provider["name"] == "cerebras"
    assert provider["base_url"] == "https://api.cerebras.ai/v1"
    assert "id" in provider
    assert "created_at" in provider
    # Response must never expose auth_secret_ref.
    assert "auth_secret_ref" not in provider


async def test_create_model_config_roundtrip(client: AsyncClient) -> None:
    provider = await _create_provider(client)
    config = await _create_model_config(client, str(provider["id"]))
    assert config["provider_id"] == provider["id"]
    assert config["model_name"] == "zai-glm-4.7"
    assert config["default_temperature"] == pytest.approx(0.7)
    assert config["supports_structured_outputs"] is True


async def test_create_model_config_unknown_provider(client: AsyncClient) -> None:
    response = await client.post(
        "/model-configs",
        json={
            "provider_id": str(uuid.uuid4()),
            "model_name": "x",
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "default_max_output_tokens": 256,
            "supports_structured_outputs": False,
        },
    )
    assert response.status_code == 422
    assert "unknown provider_id" in response.json()["detail"]


async def test_create_prompt_version_roundtrip(client: AsyncClient) -> None:
    prompt = await _create_prompt_version(client)
    assert prompt["ruleset_id"] == "mini7_v1"
    assert prompt["response_schema"] == _PROMPT_SCHEMA


async def test_create_agent_build_and_get(client: AsyncClient) -> None:
    provider = await _create_provider(client)
    config = await _create_model_config(client, str(provider["id"]))
    prompt = await _create_prompt_version(client)
    build = await _create_agent_build(client, str(config["id"]), str(prompt["id"]))

    assert build["display_name"] == "glm-mini7"
    assert build["model_config_id"] == config["id"]
    assert build["prompt_version_id"] == prompt["id"]
    assert build["inference_params"] == {"temperature": 0.5}
    assert build["active"] is True

    response = await client.get(f"/agent-builds/{build['id']}")
    assert response.status_code == 200
    fetched = response.json()
    assert fetched["id"] == build["id"]
    # Secret references from the chained model_provider must NEVER appear.
    assert "auth_secret_ref" not in fetched
    for value in fetched.values():
        assert value != "env:CEREBRAS_API_KEY"


async def test_create_agent_build_unknown_model_config(client: AsyncClient) -> None:
    prompt = await _create_prompt_version(client)
    response = await client.post(
        "/agent-builds",
        json={
            "display_name": "x",
            "model_config_id": str(uuid.uuid4()),
            "prompt_version_id": str(prompt["id"]),
            "adapter_version": "v0",
            "inference_params": {},
        },
    )
    assert response.status_code == 422
    assert "unknown model_config_id" in response.json()["detail"]


async def test_create_agent_build_unknown_prompt_version(client: AsyncClient) -> None:
    provider = await _create_provider(client)
    config = await _create_model_config(client, str(provider["id"]))
    response = await client.post(
        "/agent-builds",
        json={
            "display_name": "x",
            "model_config_id": str(config["id"]),
            "prompt_version_id": str(uuid.uuid4()),
            "adapter_version": "v0",
            "inference_params": {},
        },
    )
    assert response.status_code == 422
    assert "unknown prompt_version_id" in response.json()["detail"]


async def test_get_agent_build_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/agent-builds/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_admin_routes_rejected_when_no_session_factory() -> None:
    app = create_app()  # no session_factory wired
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/model-providers",
            json={"name": "x", "auth_secret_ref": "env:X"},
        )
    assert response.status_code == 503


async def test_create_provider_validation_rejects_empty_name(client: AsyncClient) -> None:
    response = await client.post(
        "/model-providers",
        json={"name": "", "auth_secret_ref": "env:X"},
    )
    assert response.status_code == 422


async def test_create_provider_rejects_unresolvable_secret_ref(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADRINO_NEVER_DEFINED_KEY", raising=False)
    response = await client.post(
        "/model-providers",
        json={
            "name": "broken",
            "auth_secret_ref": "env:PADRINO_NEVER_DEFINED_KEY",
        },
    )
    assert response.status_code == 422
    assert "auth_secret_ref" in response.json()["detail"]


async def test_create_provider_rejects_unknown_scheme(client: AsyncClient) -> None:
    response = await client.post(
        "/model-providers",
        json={"name": "vault-proto", "auth_secret_ref": "vault:secret/data"},
    )
    assert response.status_code == 422
    assert "scheme" in response.json()["detail"]
