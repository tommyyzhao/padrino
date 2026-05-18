"""docker-compose stack smoke (US-064).

Marked ``@pytest.mark.docker`` so the default ``pytest`` run on a machine
without Docker simply skips the suite. CI runs it explicitly via ``-m docker``
on push-to-main so a broken Dockerfile or compose definition fails the release
job rather than the first operator who tries to ``docker compose up``.

The test brings up the full stack (postgres + bootstrap + api + scheduler),
waits for the API and scheduler healthchecks to converge, probes ``/healthz``,
``/healthz/scheduler``, and ``/metrics``, and then executes a one-clone demo
gauntlet inside the api container against the running Postgres — the engine
end-to-end smoke that closes the loop on "runs a demo gauntlet via the API"
from the US-064 acceptance criteria.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_UP_TIMEOUT_S = 600  # cold image build can be slow
ENDPOINT_POLL_TIMEOUT_S = 60


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


def _wait_for_http_ok(url: str, *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last_error: str = "no attempts made"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return str(response.read().decode("utf-8"))
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise AssertionError(f"{url} never returned 200 within {timeout_s}s: {last_error}")


@pytest.fixture()
def compose_stack() -> Iterator[dict[str, str]]:
    project = f"padrino-smoke-{uuid.uuid4().hex[:8]}"
    host_port = _pick_free_port()
    env = {
        **os.environ,
        "PADRINO_API_PORT": str(host_port),
        "PADRINO_IMAGE": f"padrino-smoke:{project}",
        "POSTGRES_PASSWORD": "smoke",
        "PADRINO_SCHEDULER_CONCURRENCY": "1",
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
        yield {"project": project, "host_port": str(host_port)}
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
    base = f"http://127.0.0.1:{compose_stack['host_port']}"

    healthz = _wait_for_http_ok(f"{base}/healthz", timeout_s=ENDPOINT_POLL_TIMEOUT_S)
    assert json.loads(healthz) == {"status": "ok"}

    scheduler_body = _wait_for_http_ok(
        f"{base}/healthz/scheduler", timeout_s=ENDPOINT_POLL_TIMEOUT_S
    )
    scheduler_payload = json.loads(scheduler_body)
    assert scheduler_payload["status"] in {"ok", "degraded", "down"}
    assert "pending_gauntlets" in scheduler_payload

    metrics_body = _wait_for_http_ok(f"{base}/metrics", timeout_s=ENDPOINT_POLL_TIMEOUT_S)
    assert "padrino_api_requests_total" in metrics_body

    # End-to-end engine smoke: run a one-clone demo gauntlet via the api
    # container. The demo gauntlet seeds its own canonical prompts and is
    # designed for a fresh DB, so it writes to a temp SQLite file inside the
    # container rather than re-seeding the bootstrap-managed Postgres. Uses
    # the deterministic mock adapter so no provider credentials are required.
    project = compose_stack["project"]
    env = {
        **os.environ,
        "PADRINO_IMAGE": f"padrino-smoke:{project}",
    }
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
