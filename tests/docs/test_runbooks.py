"""Verified-runbook tests for the deployment docs (US-065).

Each file under ``docs/deployment/*.md`` ends with a "Verified runbook" section
containing one or more fenced ``bash`` code blocks whose first line is a
``# verified`` comment. These blocks are extracted and executed end-to-end so
the documented commands cannot silently rot — if a future change breaks
``padrino bootstrap`` or the ``providers.yaml`` schema, the matching docs go
red in CI immediately.

To keep the suite fast and provider-free, the extractor rewrites every
``uv run <bin>`` invocation to the project's ``.venv/bin/<bin>`` so subprocess
runs don't pay uv's project-resolution / sync cost. The semantics are
identical: both forms invoke the same console script installed by
``uv sync --all-extras``. Heavier docker-compose flows live alongside the
verified blocks but are NOT tagged ``# verified`` — they are covered by
``tests/docker/test_compose_smoke.py`` (US-064) under the ``docker`` marker.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "deployment"
VENV_BIN = REPO_ROOT / ".venv" / "bin"

DOC_FILES: tuple[str, ...] = (
    "central-backend.md",
    "self-host.md",
    "byo-model.md",
)

_BLOCK_RE = re.compile(
    r"```bash\n# verified\b[^\n]*\n(?P<body>.*?)```",
    re.DOTALL,
)
_UV_RUN_RE = re.compile(r"\buv run (\S+)")


def _extract_verified_blocks(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    return [m.group("body") for m in _BLOCK_RE.finditer(text)]


def _rewrite_uv_run(block: str) -> str:
    return _UV_RUN_RE.sub(lambda m: str(VENV_BIN / m.group(1)), block)


@pytest.mark.parametrize("doc_name", DOC_FILES)
def test_doc_has_verified_runbook(doc_name: str) -> None:
    """Each deployment doc must ship at least one ``# verified`` bash block."""
    doc = DOCS_DIR / doc_name
    assert doc.exists(), f"missing deployment doc: {doc}"
    blocks = _extract_verified_blocks(doc)
    assert blocks, f"{doc_name} has no `# verified` bash block in its Verified runbook section"


@pytest.mark.parametrize("doc_name", DOC_FILES)
def test_verified_blocks_exit_zero(doc_name: str, tmp_path: Path) -> None:
    """Run every ``# verified`` block in ``doc_name`` and assert exit 0.

    Each block runs in its own scratch directory with a freshly-derived
    ``PADRINO_DB_URL`` pointed at a SQLite file under ``tmp_path``. The
    project's ``.venv/bin`` is the only entry on ``PATH`` so the rewritten
    ``uv run X`` → ``<venv>/bin/X`` finds the installed console scripts.
    """
    doc = DOCS_DIR / doc_name
    blocks = _extract_verified_blocks(doc)
    assert blocks, f"{doc_name} has no `# verified` blocks"

    for index, block in enumerate(blocks):
        sandbox = tmp_path / f"block{index:02d}"
        sandbox.mkdir()
        rewritten = _rewrite_uv_run(block)
        script = "set -euo pipefail\n" + rewritten
        env = {
            **os.environ,
            "PADRINO_DB_URL": f"sqlite+aiosqlite:///{sandbox}/runbook.db",
            "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}",
        }
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"{doc_name} verified block #{index} exited {result.returncode}\n"
            f"---- block ----\n{block}\n"
            f"---- stdout ----\n{result.stdout}\n"
            f"---- stderr ----\n{result.stderr}\n"
        )
