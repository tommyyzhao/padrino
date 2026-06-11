"""Compose v2 graph validation (US-109).

Marked ``@pytest.mark.docker`` so the default ``pytest`` run skips this suite.
Run explicitly with ``-m docker`` to validate the compose definition without
starting any containers.

These tests parse ``docker-compose.yml`` and assert that:
- All required Wave 7 services are declared.
- The wave-7 env vars (spend cap, guard key, matchmaking toggle, cadence,
  retention TTLs) are forwarded via the shared ``x-padrino-env`` anchor.
- The dashboard service has a separate image / build context.
- The api and scheduler services share the same padrino image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

REQUIRED_SERVICES = {"postgres", "bootstrap", "api", "scheduler", "dashboard"}

WAVE7_ENV_VARS = {
    "DEEPINFRA_API_KEY",
    "PADRINO_GLOBAL_SPEND_CAP_USD",
    "PADRINO_ENABLE_CONTINUOUS_MATCHMAKING",
    "PADRINO_BROADCAST_CADENCE_CHAT_MS",
    "PADRINO_RAW_PAYLOAD_TTL_DAYS",
    "PADRINO_NON_BROADCASTABLE_GAME_TTL_DAYS",
}


@pytest.fixture(scope="module")
def compose_doc() -> dict[str, Any]:
    with COMPOSE_FILE.open() as fh:
        result: dict[str, Any] = yaml.safe_load(fh)
        return result


@pytest.mark.docker
def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.exists(), f"docker-compose.yml not found at {COMPOSE_FILE}"


@pytest.mark.docker
def test_all_required_services_declared(compose_doc: dict[str, Any]) -> None:
    declared = set(compose_doc.get("services", {}).keys())
    missing = REQUIRED_SERVICES - declared
    assert not missing, f"Missing services in docker-compose.yml: {missing}"


@pytest.mark.docker
def test_api_and_scheduler_share_padrino_image(compose_doc: dict[str, Any]) -> None:
    services = compose_doc["services"]
    api_image = services["api"].get("image", "")
    scheduler_image = services["scheduler"].get("image", "")
    assert api_image == scheduler_image, (
        f"api and scheduler should use the same image; got api={api_image!r}, "
        f"scheduler={scheduler_image!r}"
    )


@pytest.mark.docker
def test_dashboard_has_separate_build_context(compose_doc: dict[str, Any]) -> None:
    services = compose_doc["services"]
    dashboard_build = services["dashboard"].get("build", {})
    context = dashboard_build.get("context", "")
    assert "dashboard" in context.lower(), (
        f"dashboard build context should reference the dashboard directory; got {context!r}"
    )


@pytest.mark.docker
def test_wave7_env_vars_present_in_compose(compose_doc: dict[str, Any]) -> None:
    raw = COMPOSE_FILE.read_text()
    missing = [var for var in WAVE7_ENV_VARS if var not in raw]
    assert not missing, (
        f"Wave 7 env vars missing from docker-compose.yml: {missing}. "
        "Add them to the x-padrino-env anchor."
    )


@pytest.mark.docker
def test_bootstrap_depends_on_postgres(compose_doc: dict[str, Any]) -> None:
    deps = compose_doc["services"]["bootstrap"].get("depends_on", {})
    assert "postgres" in deps, "bootstrap should declare postgres as a dependency"


@pytest.mark.docker
def test_api_depends_on_bootstrap(compose_doc: dict[str, Any]) -> None:
    deps = compose_doc["services"]["api"].get("depends_on", {})
    assert "bootstrap" in deps, "api should depend on bootstrap (migrations gate)"


@pytest.mark.docker
def test_scheduler_depends_on_api(compose_doc: dict[str, Any]) -> None:
    deps = compose_doc["services"]["scheduler"].get("depends_on", {})
    assert "api" in deps, "scheduler should depend on api (healthcheck gate)"


@pytest.mark.docker
def test_postgres_has_named_volume(compose_doc: dict[str, Any]) -> None:
    volumes = compose_doc.get("volumes", {})
    assert volumes, "compose file should declare at least one named volume for postgres data"


@pytest.mark.docker
def test_api_exposes_port_8000(compose_doc: dict[str, Any]) -> None:
    ports = compose_doc["services"]["api"].get("ports", [])
    port_strings = [str(p) for p in ports]
    assert any("8000" in p for p in port_strings), "api service should expose port 8000"
