"""Tests for the Settings module (US-001)."""

from __future__ import annotations

import pytest

from padrino.settings import Settings, get_settings


def _fresh() -> Settings:
    """Return a Settings instance that reads only from the environment (no .env file).

    Bypasses both the lru_cache and the on-disk .env so monkeypatch controls everything.
    """
    return Settings(_env_file=None)


def test_default_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_DB_URL", raising=False)
    assert _fresh().padrino_db_url == "sqlite+aiosqlite:///./padrino.db"


def test_default_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_LOG_LEVEL", raising=False)
    assert _fresh().padrino_log_level == "INFO"


def test_default_llm_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_LLM_TIMEOUT_SECONDS", raising=False)
    assert _fresh().padrino_llm_timeout_seconds == 45


def test_default_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_TEMPERATURE", raising=False)
    assert _fresh().padrino_temperature == 0.7


def test_default_top_p(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_TOP_P", raising=False)
    assert _fresh().padrino_top_p == 1.0


def test_default_primary_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_PRIMARY_MODEL", raising=False)
    assert _fresh().padrino_primary_model == "cerebras/zai-glm-4.7"


def test_default_fallback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_FALLBACK_MODEL", raising=False)
    assert _fresh().padrino_fallback_model == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"


def test_env_override_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_LOG_LEVEL", "DEBUG")
    assert _fresh().padrino_log_level == "DEBUG"


def test_env_override_primary_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_PRIMARY_MODEL", "openai/gpt-4")
    assert _fresh().padrino_primary_model == "openai/gpt-4"


def test_env_override_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_LLM_TIMEOUT_SECONDS", "99")
    assert _fresh().padrino_llm_timeout_seconds == 99


def test_get_settings_returns_same_instance() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_api_keys_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    s = _fresh()
    assert s.cerebras_api_key is None
    assert s.deepinfra_api_key is None
    assert s.zai_api_key is None


# ---------------------------------------------------------------------------
# US-079: Settings.build_routing_policy()
# ---------------------------------------------------------------------------


def test_build_routing_policy_injects_zai_glm47_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
    monkeypatch.setenv("PADRINO_CEREBRAS_ZAI_GLM47_ZAI_FALLBACK", "true")
    policy = _fresh().build_routing_policy()
    assert policy.primary_model == "cerebras/zai-glm-4.7"
    assert policy.fallback_model == "deepinfra/deepseek-ai/DeepSeek-V4-Flash"
    assert len(policy.same_model_hosts) == 1
    host = policy.same_model_hosts[0]
    assert host.provider == "zai"
    assert host.litellm_model_id == "openai/glm-4.7"
    assert host.api_base == "https://api.z.ai/api/coding/paas/v4"
    assert host.auth_secret_ref == "env:ZAI_API_KEY"


def test_build_routing_policy_omits_same_model_hosts_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
    monkeypatch.setenv("PADRINO_CEREBRAS_ZAI_GLM47_ZAI_FALLBACK", "false")
    policy = _fresh().build_routing_policy()
    assert policy.same_model_hosts == ()


def test_build_routing_policy_omits_same_model_hosts_when_zai_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("PADRINO_CEREBRAS_ZAI_GLM47_ZAI_FALLBACK", "true")
    policy = _fresh().build_routing_policy()
    assert policy.same_model_hosts == ()


def test_build_routing_policy_omits_same_model_hosts_when_primary_not_cerebras_glm47(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
    monkeypatch.setenv("PADRINO_CEREBRAS_ZAI_GLM47_ZAI_FALLBACK", "true")
    policy = _fresh().build_routing_policy(primary_model="openai/gpt-4")
    assert policy.same_model_hosts == ()
