"""US-230 compose-shape guard for the human-lane worker service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def _compose_doc() -> dict[str, Any]:
    with COMPOSE_FILE.open() as fh:
        result: dict[str, Any] = yaml.safe_load(fh)
        return result


def test_human_lane_compose_service_shape() -> None:
    compose_doc = _compose_doc()
    services = compose_doc["services"]
    human_lane = services["human-lane"]

    assert human_lane.get("image") == services["api"].get("image"), (
        "human-lane should reuse the same padrino image as api"
    )
    assert human_lane.get("command", [])[0] == "human-lane", (
        "human-lane service must run the `padrino human-lane` entrypoint"
    )
    assert human_lane.get("restart") == services["scheduler"].get("restart"), (
        "human-lane restart policy should match scheduler"
    )

    deps = human_lane.get("depends_on", {})
    assert deps.get("postgres", {}).get("condition") == "service_healthy"
    assert deps.get("bootstrap", {}).get("condition") == "service_completed_successfully"
    assert deps.get("api", {}).get("condition") == "service_healthy"

    assert not human_lane.get("ports", []), "human-lane must not publish a host port"
    assert human_lane.get("networks", []) == ["internal"], (
        "human-lane must be internal-network-only"
    )

    healthcheck = human_lane.get("healthcheck", {})
    health_text = str(healthcheck.get("test", ""))
    assert "/healthz/human-lane" in health_text
    assert "'status')=='ok'" in health_text
