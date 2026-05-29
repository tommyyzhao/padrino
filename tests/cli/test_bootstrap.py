"""Tests for the ``padrino bootstrap`` CLI command (US-058).

Covers the full bootstrap pipeline against a fresh SQLite database:

- end-to-end fresh DB lands every step at ``ok``
- re-running is a no-op (every seeding step reports ``skipped``)
- ``--providers`` YAML happy path registers the listed providers
- ``--providers`` YAML with an unresolvable secret ref fails fast at the
  providers step (no provider row written)
- ``--with-admin-key`` prints exactly one raw key and stores its sha256
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from padrino.bootstrap import (
    ADMIN_KEY_LABEL,
    DEFAULT_LEAGUE_NAME,
    STEP_CANONICAL_PROMPTS,
    STEP_DEFAULT_LEAGUE,
    STEP_MIGRATIONS,
    STEP_PROVIDERS,
)
from padrino.cli import app
from padrino.core.rulesets import mini7_v1
from padrino.db.base import create_engine, create_session_factory
from padrino.db.models import ApiKey, League, ModelConfig, ModelProvider, PromptVersion


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'padrino.db'}"


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [s for s in steps if s["name"] == name]
    assert matches, f"step {name!r} missing from {steps!r}"
    return matches[0]


async def _query(
    db_url: str,
) -> tuple[list[PromptVersion], list[League], list[ApiKey], list[ModelProvider]]:
    engine = create_engine(db_url)
    try:
        sf = create_session_factory(engine)
        async with sf() as session:
            prompts = list(
                (
                    await session.execute(
                        select(PromptVersion).where(PromptVersion.version == "canonical_mini7_v1")
                    )
                ).scalars()
            )
            leagues = list(
                (
                    await session.execute(select(League).where(League.name == DEFAULT_LEAGUE_NAME))
                ).scalars()
            )
            keys = list((await session.execute(select(ApiKey))).scalars())
            providers = list((await session.execute(select(ModelProvider))).scalars())
    finally:
        await engine.dispose()
    return prompts, leagues, keys, providers


async def _query_model_configs(db_url: str) -> list[ModelConfig]:
    engine = create_engine(db_url)
    try:
        sf = create_session_factory(engine)
        async with sf() as session:
            models = list((await session.execute(select(ModelConfig))).scalars())
    finally:
        await engine.dispose()
    return models


def test_bootstrap_fresh_db_runs_every_step(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", "--db-url", _db_url(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert payload["succeeded"] is True

    names = {s["name"] for s in payload["steps"]}
    assert names == {STEP_MIGRATIONS, STEP_CANONICAL_PROMPTS, STEP_DEFAULT_LEAGUE}
    assert _step(payload["steps"], STEP_MIGRATIONS)["status"] == "ok"
    # Migration 0005 already seeded the four canonical mini7_v1 rows, but we now seed bench10_v1 rows on fresh boot, so the safety-net
    # step reports "ok" on a fresh DB.
    assert _step(payload["steps"], STEP_CANONICAL_PROMPTS)["status"] == "ok"
    assert _step(payload["steps"], STEP_DEFAULT_LEAGUE)["status"] == "ok"
    assert "admin_raw_key" not in payload


def test_bootstrap_seeds_canonical_prompts_and_default_league(tmp_path: Path) -> None:
    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(app, ["bootstrap", "--db-url", db_url])
    assert result.exit_code == 0, result.stdout

    prompts, leagues, keys, providers = asyncio.run(_query(db_url))
    families = {p.developer_prompt for p in prompts}
    assert families == {"DECEPTIVE", "INVESTIGATIVE", "PROTECTIVE", "VANILLA_TOWN"}
    assert len(leagues) == 1
    assert leagues[0].ruleset_id == mini7_v1.RULESET_ID
    assert leagues[0].ranked is True
    assert keys == []
    assert providers == []


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    runner = CliRunner()
    db_url = _db_url(tmp_path)

    first = runner.invoke(app, ["bootstrap", "--db-url", db_url])
    assert first.exit_code == 0, first.stdout

    second = runner.invoke(app, ["bootstrap", "--db-url", db_url])
    assert second.exit_code == 0, second.stdout

    payload = json.loads(second.stdout)
    assert payload["succeeded"] is True
    # Every seeding step skips because the rows already exist.
    assert _step(payload["steps"], STEP_CANONICAL_PROMPTS)["status"] == "skipped"
    assert _step(payload["steps"], STEP_DEFAULT_LEAGUE)["status"] == "skipped"


def test_bootstrap_with_admin_key_prints_one_raw_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        ["bootstrap", "--db-url", db_url, "--with-admin-key"],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert payload["succeeded"] is True

    raw_key = payload["admin_raw_key"]
    assert isinstance(raw_key, str) and raw_key.startswith("pk_")

    _, _, keys, _ = asyncio.run(_query(db_url))
    assert len(keys) == 1
    stored = keys[0]
    assert stored.label == ADMIN_KEY_LABEL
    assert stored.scopes == ["admin"]
    assert stored.key_hash == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    # The raw key must never be persisted; only its prefix surfaces in the DB.
    assert stored.key_prefix == raw_key[:6]
    # Re-running with the flag mints a second key — keys are not deduplicated.
    second = runner.invoke(
        app,
        ["bootstrap", "--db-url", db_url, "--with-admin-key"],
    )
    assert second.exit_code == 0, second.stdout
    _, _, keys_after, _ = asyncio.run(_query(db_url))
    assert len(keys_after) == 2


def test_bootstrap_with_valid_providers_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOTSTRAP_TEST_KEY", "shhh-not-real")
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: test-cerebras
                auth_secret_ref: env:BOOTSTRAP_TEST_KEY
                base_url: https://api.cerebras.ai
                default_model: zai-glm-4.7
                timeout_s: 30.0
              - name: test-deepinfra
                auth_secret_ref: env:BOOTSTRAP_TEST_KEY
            """
        ).strip()
    )
    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    providers_step = _step(payload["steps"], STEP_PROVIDERS)
    assert providers_step["status"] == "ok"
    assert sorted(providers_step["detail"]["inserted"]) == [
        "test-cerebras",
        "test-deepinfra",
    ]
    _, _, _, providers = asyncio.run(_query(db_url))
    names = {p.name for p in providers}
    assert names == {"test-cerebras", "test-deepinfra"}

    # Re-running with the same YAML skips the already-registered names.
    second = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert second.exit_code == 0, second.stdout
    payload_2 = json.loads(second.stdout)
    providers_step_2 = _step(payload_2["steps"], STEP_PROVIDERS)
    assert providers_step_2["status"] == "skipped"
    assert providers_step_2["detail"]["inserted"] == []
    assert sorted(providers_step_2["detail"]["skipped"]) == [
        "test-cerebras",
        "test-deepinfra",
    ]


def test_bootstrap_with_xiaomi_models_yaml_seeds_model_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XIAOMI_API_KEY", "tp-not-real-but-resolves")
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: xiaomi
                auth_secret_ref: env:XIAOMI_API_KEY
                base_url: https://token-plan-sgp.xiaomimimo.com/v1
                models:
                  - model_name: mimo-v2.5
                    litellm_model_id: openai/mimo-v2.5
                  - model_name: mimo-v2.5-pro
                    litellm_model_id: openai/mimo-v2.5-pro
            """
        ).strip()
    )

    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    providers_step = _step(payload["steps"], STEP_PROVIDERS)
    assert providers_step["detail"]["inserted"] == ["xiaomi"]
    assert sorted(providers_step["detail"]["models_inserted"]) == [
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2.5-pro",
    ]

    _, _, _, providers = asyncio.run(_query(db_url))
    assert len(providers) == 1
    assert providers[0].name == "xiaomi"
    assert providers[0].base_url == "https://token-plan-sgp.xiaomimimo.com/v1"

    models = asyncio.run(_query_model_configs(db_url))
    by_name = {m.model_name: m for m in models}
    assert set(by_name) == {"mimo-v2.5", "mimo-v2.5-pro"}
    assert by_name["mimo-v2.5"].litellm_model_id == "openai/mimo-v2.5"
    assert by_name["mimo-v2.5-pro"].litellm_model_id == "openai/mimo-v2.5-pro"
    assert all(m.default_temperature == pytest.approx(0.7) for m in models)
    assert all(m.default_top_p == pytest.approx(1.0) for m in models)
    assert all(m.default_max_output_tokens == 4096 for m in models)

    second = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert second.exit_code == 0, second.stdout
    payload_2 = json.loads(second.stdout)
    providers_step_2 = _step(payload_2["steps"], STEP_PROVIDERS)
    assert providers_step_2["status"] == "skipped"
    assert providers_step_2["detail"]["models_inserted"] == []
    assert sorted(providers_step_2["detail"]["models_skipped"]) == [
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2.5-pro",
    ]


def test_bootstrap_with_zai_glm51_yaml_seeds_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "0123456789abcdef0123456789abcdef.ABCDEFGHIJKLMNO1")
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: zai
                auth_secret_ref: env:ZAI_API_KEY
                base_url: https://api.z.ai/api/coding/paas/v4
                models:
                  - model_name: glm-5.1
                    litellm_model_id: openai/glm-5.1
            """
        ).strip()
    )

    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    providers_step = _step(payload["steps"], STEP_PROVIDERS)
    assert providers_step["detail"]["inserted"] == ["zai"]
    assert providers_step["detail"]["models_inserted"] == ["zai/glm-5.1"]

    _, _, _, providers = asyncio.run(_query(db_url))
    assert len(providers) == 1
    assert providers[0].name == "zai"
    assert providers[0].base_url == "https://api.z.ai/api/coding/paas/v4"
    assert providers[0].auth_secret_ref == "env:ZAI_API_KEY"

    models = asyncio.run(_query_model_configs(db_url))
    assert len(models) == 1
    assert models[0].model_name == "glm-5.1"
    assert models[0].litellm_model_id == "openai/glm-5.1"
    assert models[0].default_temperature == pytest.approx(0.7)
    assert models[0].default_top_p == pytest.approx(1.0)
    assert models[0].default_max_output_tokens == 4096


def test_bootstrap_with_deepinfra_gemma_yaml_seeds_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPINFRA_API_KEY", "lw-not-real-but-resolves")
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: deepinfra
                auth_secret_ref: env:DEEPINFRA_API_KEY
                base_url: https://api.deepinfra.com/v1/openai
                models:
                  - model_name: deepseek-ai/DeepSeek-V4-Flash
                    litellm_model_id: deepinfra/deepseek-ai/DeepSeek-V4-Flash
                  - model_name: gemma-4-26B-A4B-it
                    litellm_model_id: deepinfra/google/gemma-4-26B-A4B-it
            """
        ).strip()
    )

    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    providers_step = _step(payload["steps"], STEP_PROVIDERS)
    assert providers_step["detail"]["inserted"] == ["deepinfra"]
    assert sorted(providers_step["detail"]["models_inserted"]) == [
        "deepinfra/deepseek-ai/DeepSeek-V4-Flash",
        "deepinfra/gemma-4-26B-A4B-it",
    ]

    models = asyncio.run(_query_model_configs(db_url))
    by_name = {m.model_name: m for m in models}
    assert set(by_name) == {"deepseek-ai/DeepSeek-V4-Flash", "gemma-4-26B-A4B-it"}
    assert by_name["gemma-4-26B-A4B-it"].litellm_model_id == "deepinfra/google/gemma-4-26B-A4B-it"
    assert by_name["gemma-4-26B-A4B-it"].default_temperature == pytest.approx(0.7)
    assert by_name["gemma-4-26B-A4B-it"].default_top_p == pytest.approx(1.0)
    assert by_name["gemma-4-26B-A4B-it"].default_max_output_tokens == 4096


def test_bootstrap_with_invalid_secret_ref_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOOTSTRAP_MISSING_KEY", raising=False)
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: test-missing
                auth_secret_ref: env:BOOTSTRAP_MISSING_KEY
            """
        ).strip()
    )
    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 1, result.stdout

    payload = json.loads(result.stdout)
    assert payload["succeeded"] is False
    assert payload["failed_step"] == STEP_PROVIDERS
    assert "BOOTSTRAP_MISSING_KEY" in payload["failure_message"]

    _, _, _, providers = asyncio.run(_query(db_url))
    assert providers == []


def test_bootstrap_with_unknown_yaml_key_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOTSTRAP_TEST_KEY", "shhh-not-real")
    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: bad-key
                auth_secret_ref: env:BOOTSTRAP_TEST_KEY
                unknown_field: nope
            """
        ).strip()
    )
    runner = CliRunner()
    db_url = _db_url(tmp_path)
    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--db-url",
            db_url,
            "--providers",
            str(providers_path),
        ],
    )
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["failed_step"] == STEP_PROVIDERS
    assert "unknown_field" in payload["failure_message"]
