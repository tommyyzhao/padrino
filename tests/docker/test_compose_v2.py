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
def test_api_exposes_no_published_port(compose_doc: dict[str, Any]) -> None:
    """US-119: the full api is internal-only; only public-api faces the edge."""
    ports = compose_doc["services"]["api"].get("ports", [])
    assert not ports, (
        "the full api service must NOT publish a host port in the public/private "
        "split topology; only the public-api service is edge-facing"
    )


# --- US-119: public edge vs private backend split topology -------------------


@pytest.mark.docker
def test_public_api_service_declared(compose_doc: dict[str, Any]) -> None:
    services = compose_doc.get("services", {})
    assert "public-api" in services, (
        "docker-compose.yml must declare a public-api (surface-only) service"
    )


@pytest.mark.docker
def test_public_api_runs_surface_only(compose_doc: dict[str, Any]) -> None:
    env = compose_doc["services"]["public-api"].get("environment", {})
    assert env.get("PADRINO_PUBLIC_SURFACE_ONLY") == "true", (
        "public-api must set PADRINO_PUBLIC_SURFACE_ONLY=true so it physically "
        "cannot mount private routers"
    )


@pytest.mark.docker
def test_public_api_publishes_a_port(compose_doc: dict[str, Any]) -> None:
    ports = compose_doc["services"]["public-api"].get("ports", [])
    port_strings = [str(p) for p in ports]
    assert any("8000" in p for p in port_strings), (
        "public-api must publish a host port (it is the edge-facing surface)"
    )


@pytest.mark.docker
def test_postgres_publishes_no_port(compose_doc: dict[str, Any]) -> None:
    ports = compose_doc["services"]["postgres"].get("ports", [])
    assert not ports, "postgres must not publish a host port (internal network only)"


@pytest.mark.docker
def test_scheduler_publishes_no_port(compose_doc: dict[str, Any]) -> None:
    ports = compose_doc["services"]["scheduler"].get("ports", [])
    assert not ports, "scheduler must not publish a host port (internal network only)"


@pytest.mark.docker
def test_compose_declares_edge_and_internal_networks(compose_doc: dict[str, Any]) -> None:
    networks = compose_doc.get("networks", {})
    assert "edge" in networks, "compose must declare an `edge` network"
    assert "internal" in networks, "compose must declare an `internal` network"


@pytest.mark.docker
def test_public_api_on_both_networks(compose_doc: dict[str, Any]) -> None:
    nets = compose_doc["services"]["public-api"].get("networks", [])
    assert "edge" in nets, "public-api must be on the edge network"
    assert "internal" in nets, "public-api must reach the backend over internal"


@pytest.mark.docker
def test_dashboard_on_edge_network(compose_doc: dict[str, Any]) -> None:
    nets = compose_doc["services"]["dashboard"].get("networks", [])
    assert "edge" in nets, "dashboard must be on the edge network"


@pytest.mark.docker
def test_backend_services_not_on_edge(compose_doc: dict[str, Any]) -> None:
    """postgres/api/scheduler must never be reachable from the edge network."""
    services = compose_doc["services"]
    for name in ("postgres", "api", "scheduler"):
        nets = services[name].get("networks", [])
        assert "edge" not in nets, f"{name} must NOT be on the edge network"
        assert "internal" in nets, f"{name} must be on the internal network"


@pytest.mark.docker
def test_public_api_shares_padrino_image(compose_doc: dict[str, Any]) -> None:
    services = compose_doc["services"]
    assert services["public-api"].get("image") == services["api"].get("image"), (
        "public-api should reuse the same padrino image as api"
    )


@pytest.mark.docker
def test_dashboard_built_against_public_api(compose_doc: dict[str, Any]) -> None:
    """The dashboard's API base URL must point at the public-api, not the private api."""
    dashboard = compose_doc["services"]["dashboard"]
    build_args = dashboard.get("build", {}).get("args", {})
    base_url = str(build_args.get("VITE_PADRINO_API_BASE_URL", ""))
    assert "public-api" in base_url or "${" in base_url, (
        "dashboard must be built against the public-api URL "
        f"(got {base_url!r}); the default must reference public-api"
    )
