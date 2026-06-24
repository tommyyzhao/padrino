"""docker-compose stack smoke (US-064).

Marked ``@pytest.mark.docker`` so the default ``pytest`` run on a machine
without Docker simply skips the suite. CI runs it explicitly via ``-m docker``
on push-to-main so a broken Dockerfile or compose definition fails the release
job rather than the first operator who tries to ``docker compose up``.

The test brings up the full stack (postgres + bootstrap + api + scheduler +
human-lane), waits for the healthchecks to converge, probes ``/healthz``,
``/healthz/scheduler``, and ``/metrics``, and then executes a one-clone demo
gauntlet inside the api container against the running Postgres — the engine
end-to-end smoke that closes the loop on "runs a demo gauntlet via the API"
from the US-064 acceptance criteria.

US-119 split the topology so the full api/scheduler/metrics surface is
internal-only (no published host port); only ``public-api`` (surface-only) and
``dashboard`` are edge-published. The private endpoints are therefore probed
from inside the api container via ``docker compose exec`` rather than across a
host port, and the edge ports are bound to ephemeral host ports so the smoke
run never collides with whatever is already on 8000/5173.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_UP_TIMEOUT_S = 600  # cold image build can be slow


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker_bin() -> str:
    docker_bin = shutil.which("docker")
    assert docker_bin is not None, "docker binary should be available — marker auto-skips otherwise"
    return docker_bin


def _compose_cmd(project: str) -> list[str]:
    return [_docker_bin(), "compose", "--project-name", project]


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _exec_fetch(project: str, env: dict[str, str], path: str) -> str:
    """Fetch an internal HTTP endpoint from inside the api container.

    US-119 makes the full api/scheduler/metrics surface internal-only (no
    published host port), so the smoke test probes these endpoints from within
    the running api container rather than across a host port binding. ``up
    --wait`` already blocked on the api + scheduler healthchecks, so the
    endpoints are live by the time this runs.
    """
    code = (
        "import sys,urllib.request as u;"
        f"sys.stdout.write(u.urlopen('http://127.0.0.1:8000{path}',timeout=10)"
        ".read().decode())"
    )
    result = _run(
        [*_compose_cmd(project), "exec", "-T", "api", "python", "-c", code],
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
    )
    return result.stdout


@pytest.fixture()
def compose_stack() -> Iterator[dict[str, str]]:
    project = f"padrino-smoke-{uuid.uuid4().hex[:8]}"
    env = {
        **os.environ,
        "PADRINO_IMAGE": f"padrino-smoke:{project}",
        "POSTGRES_PASSWORD": "smoke",
        "PADRINO_SCHEDULER_CONCURRENCY": "1",
        # Only public-api + dashboard are edge-published (US-119). Bind them to
        # ephemeral host ports so the smoke run never collides with whatever is
        # already listening on the default 8000/5173.
        "PADRINO_PUBLIC_API_PORT": str(_pick_free_port()),
        "PADRINO_DASHBOARD_PORT": str(_pick_free_port()),
    }
    base_cmd = _compose_cmd(project)
    up_cmd = [
        *base_cmd,
        "up",
        "--build",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(STACK_UP_TIMEOUT_S),
    ]
    try:
        _run(up_cmd, cwd=REPO_ROOT, env=env, timeout=STACK_UP_TIMEOUT_S + 60)
        yield {"project": project}
    finally:
        _run(
            [*base_cmd, "down", "--volumes", "--remove-orphans"],
            cwd=REPO_ROOT,
            env=env,
            timeout=120,
            check=False,
        )


@pytest.mark.docker
def test_compose_stack_serves_endpoints_and_runs_demo_gauntlet(
    compose_stack: dict[str, str],
) -> None:
    project = compose_stack["project"]
    env = {
        **os.environ,
        "PADRINO_IMAGE": f"padrino-smoke:{project}",
    }

    healthz = _exec_fetch(project, env, "/healthz")
    assert json.loads(healthz) == {"status": "ok"}

    scheduler_payload = json.loads(_exec_fetch(project, env, "/healthz/scheduler"))
    assert scheduler_payload["status"] in {"ok", "degraded", "down"}
    assert "pending_gauntlets" in scheduler_payload

    metrics_body = _exec_fetch(project, env, "/metrics")
    assert "padrino_api_requests_total" in metrics_body

    # End-to-end engine smoke: run a one-clone demo gauntlet via the api
    # container. The demo gauntlet seeds its own canonical prompts and is
    # designed for a fresh DB, so it writes to a temp SQLite file inside the
    # container rather than re-seeding the bootstrap-managed Postgres. Uses
    # the deterministic mock adapter so no provider credentials are required.
    exec_result = _run(
        [
            *_compose_cmd(project),
            "exec",
            "-T",
            "api",
            "padrino",
            "demo-gauntlet",
            "--clones",
            "1",
            "--db-url",
            "sqlite+aiosqlite:////tmp/padrino-smoke-demo.db",
        ],
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )
    payload = json.loads(exec_result.stdout)
    assert "entries" in payload, payload
    assert payload.get("ruleset_id") == "mini7_v1"
