"""Tests for the Settings module (US-001)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from starlette.requests import Request

from padrino.api.routes.lobbies import ranked_human_rating_enabled_from_request
from padrino.ratings.human_openskill_service import ranked_human_rating_enabled
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


def test_default_human_lane_mock_ai_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_HUMAN_LANE_MOCK_AI", raising=False)
    assert _fresh().padrino_human_lane_mock_ai is False


def test_default_ranked_human_rating_kill_switch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADRINO_RANKED_HUMAN_RATING_ENABLED", raising=False)
    assert _fresh().padrino_ranked_human_rating_enabled is False


def test_ranked_human_rating_kill_switch_visible_to_api_and_ratings_layers() -> None:
    settings = Settings(_env_file=None, padrino_ranked_human_rating_enabled=True)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/lobbies",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(auth_settings=settings)),
        }
    )

    assert ranked_human_rating_enabled_from_request(request) is True
    assert ranked_human_rating_enabled(settings) is True


def test_ranked_human_rating_kill_switch_fails_closed_for_missing_settings() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/lobbies",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(auth_settings=object())),
        }
    )

    assert ranked_human_rating_enabled_from_request(request) is False
    assert ranked_human_rating_enabled(object()) is False


def test_core_does_not_import_settings_or_credential_crypto() -> None:
    forbidden = ("padrino.settings", "cryptography")
    offenders = {
        module: [
            path
            for path in Path("src/padrino/core").rglob("*.py")
            if module in path.read_text(encoding="utf-8")
        ]
        for module in forbidden
    }
    assert offenders == {module: [] for module in forbidden}


def test_env_override_human_lane_mock_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_HUMAN_LANE_MOCK_AI", "1")
    assert _fresh().padrino_human_lane_mock_ai is True


def test_env_override_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_LOG_LEVEL", "DEBUG")
    assert _fresh().padrino_log_level == "DEBUG"


def test_env_override_primary_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_PRIMARY_MODEL", "openai/gpt-4")
    assert _fresh().padrino_primary_model == "openai/gpt-4"


def test_env_override_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_LLM_TIMEOUT_SECONDS", "99")
    assert _fresh().padrino_llm_timeout_seconds == 99


def test_human_cost_defaults_are_single_host_sized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human-lane cost defaults should fit the single-host rollout budget."""
    monkeypatch.delenv("PADRINO_HUMAN_MAX_GAMES_PER_USER_PER_DAY", raising=False)
    monkeypatch.delenv("PADRINO_HUMAN_MAX_JOINS_PER_USER_PER_DAY", raising=False)
    monkeypatch.delenv("PADRINO_HUMAN_MAX_INFERENCE_USD_PER_USER_PER_DAY", raising=False)
    monkeypatch.delenv("PADRINO_HUMAN_LOBBY_COST_CAP_USD", raising=False)
    monkeypatch.delenv("PADRINO_HUMAN_GLOBAL_LOBBY_COST_BREAKER_USD", raising=False)
    monkeypatch.delenv("PADRINO_HUMAN_ADMISSION_INFERENCE_RESERVE_USD", raising=False)

    settings = _fresh()

    assert settings.padrino_human_max_games_per_user_per_day == 20
    assert settings.padrino_human_max_joins_per_user_per_day == 60
    assert settings.padrino_human_max_inference_usd_per_user_per_day == 25.0
    assert settings.padrino_human_lobby_cost_cap_usd == 12.0
    assert settings.padrino_human_global_lobby_cost_breaker_usd == 1000.0
    assert settings.padrino_human_admission_inference_reserve_usd == 0.5


def test_human_fallback_token_prices_cover_configured_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default fallback prices should cover the model strings Padrino routes."""
    monkeypatch.delenv("PADRINO_HUMAN_FALLBACK_TOKEN_PRICE_PER_1K", raising=False)
    table = _fresh().padrino_human_fallback_token_price_per_1k

    assert table["default"] == (0.0005, 0.0015)
    assert table["cerebras/zai-glm-4.7"] == (0.00225, 0.00275)
    assert table["openai/glm-4.7"] == (0.0006, 0.0022)
    assert table["deepinfra/deepseek-ai/DeepSeek-V4-Flash"] == (0.0001, 0.0002)
    assert table["deepinfra/google/gemma-4-26B-A4B-it"] == (0.00007, 0.00034)
    assert table["openai/mimo-v2.5"] == (0.00014, 0.00028)
    assert table["openai/mimo-v2.5-pro"] == (0.000435, 0.00087)


def test_env_override_human_global_cost_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_HUMAN_GLOBAL_LOBBY_COST_BREAKER_USD", "1234.5")
    assert _fresh().padrino_human_global_lobby_cost_breaker_usd == 1234.5


def test_get_settings_returns_same_instance() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_api_keys_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("XIAOMI_API_KEY", raising=False)
    s = _fresh()
    assert s.cerebras_api_key is None
    assert s.deepinfra_api_key is None
    assert s.zai_api_key is None
    assert s.xiaomi_api_key is None


def test_default_xiaomi_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XIAOMI_BASE_URL", raising=False)
    assert _fresh().xiaomi_base_url == "https://token-plan-sgp.xiaomimimo.com/v1"


def test_default_oauth_max_token_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_OAUTH_MAX_TOKEN_AGE_SECONDS", raising=False)
    assert _fresh().padrino_oauth_max_token_age_seconds == 900


def test_env_override_oauth_max_token_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_OAUTH_MAX_TOKEN_AGE_SECONDS", "1200")
    assert _fresh().padrino_oauth_max_token_age_seconds == 1200


def test_env_none_disables_oauth_max_token_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_OAUTH_MAX_TOKEN_AGE_SECONDS", "none")
    assert _fresh().padrino_oauth_max_token_age_seconds is None


def test_env_override_xiaomi_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOMI_BASE_URL", "https://xiaomi.example.test/v1")
    assert _fresh().xiaomi_base_url == "https://xiaomi.example.test/v1"


# ---------------------------------------------------------------------------
# US-341: OpenRouter deployment configuration
# ---------------------------------------------------------------------------


def test_openrouter_settings_are_safe_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PADRINO_OPENROUTER_INFERENCE_KEY_REF",
        "PADRINO_OPENROUTER_MANAGEMENT_KEY_REF",
        "PADRINO_OPENROUTER_BASE_URL",
        "PADRINO_OPENROUTER_APP_URL",
        "PADRINO_OPENROUTER_APP_TITLE",
        "PADRINO_OPENROUTER_DEFAULT_ZDR",
        "PADRINO_OPENROUTER_TIMEOUT_SECONDS",
        "PADRINO_OPENROUTER_CATALOG_SYNC_INTERVAL_SECONDS",
        "PADRINO_OPENROUTER_CONNECT_CALLBACK_URL",
        "PADRINO_OPENROUTER_CONNECT_STATE_SIGNING_KEY",
        "PADRINO_OPENROUTER_CONNECT_FLOW_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = _fresh()

    assert settings.padrino_openrouter_inference_key_ref is None
    assert settings.padrino_openrouter_management_key_ref is None
    assert settings.padrino_openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.padrino_openrouter_app_url == "https://github.com/tommyyzhao/padrino"
    assert settings.padrino_openrouter_app_title == "Padrino"
    assert settings.padrino_openrouter_default_zdr is True
    assert settings.padrino_openrouter_timeout_seconds == 45.0
    assert settings.padrino_openrouter_catalog_sync_interval_seconds == 3600
    assert settings.padrino_openrouter_connect_callback_url is None
    assert settings.padrino_openrouter_connect_state_signing_key is None
    assert settings.padrino_openrouter_connect_flow_ttl_seconds == 600
    assert settings.padrino_openrouter_pkce_flow_retention_ttl_days == 1
    assert settings.padrino_openrouter_revoked_credential_retention_ttl_days == 30


def test_openrouter_key_settings_require_opaque_secret_references() -> None:
    with pytest.raises(ValidationError, match="opaque secret reference"):
        Settings(
            _env_file=None,
            padrino_openrouter_inference_key_ref="sk-live-secret-must-not-be-configured-here",
        )
    with pytest.raises(ValidationError, match="opaque secret reference"):
        Settings(
            _env_file=None,
            padrino_openrouter_management_key_ref="sk-live-secret-must-not-be-configured-here",
        )


def test_openrouter_connection_settings_require_https_callback_and_redact_state_key() -> None:
    state_key = "openrouter-connect-state-key-must-not-be-serialized"
    settings = Settings(
        _env_file=None,
        padrino_openrouter_connect_callback_url=(
            "https://padrino.example.test/human/openrouter/connect/callback"
        ),
        padrino_openrouter_connect_state_signing_key=SecretStr(state_key),
    )
    assert settings.padrino_openrouter_connect_state_signing_key is not None
    assert settings.padrino_openrouter_connect_state_signing_key.get_secret_value() == state_key
    assert state_key not in repr(settings)
    assert state_key not in settings.model_dump_json()

    with pytest.raises(ValidationError, match="exact HTTPS URL"):
        Settings(
            _env_file=None,
            padrino_openrouter_connect_callback_url=(
                "http://padrino.example.test/human/openrouter/connect/callback"
            ),
        )


def test_openrouter_routing_policy_uses_human_safe_casual_defaults() -> None:
    settings = Settings(
        _env_file=None,
        padrino_openrouter_app_url="https://padrino.example.test",
        padrino_openrouter_app_title="Padrino Test",
    )

    routing = settings.build_openrouter_routing_policy("meta-llama/llama-3.3-70b-instruct")

    assert routing.primary_model == "meta-llama/llama-3.3-70b-instruct"
    assert routing.fallback_model is None
    assert routing.openrouter is not None
    assert routing.openrouter.app_url == "https://padrino.example.test"
    assert routing.openrouter.app_title == "Padrino Test"
    assert routing.openrouter.provider.allow_fallbacks is True
    assert routing.openrouter.provider.zdr is True


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
