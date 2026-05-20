"""US-076 — secret-audit shell script behavioural tests.

These tests build a throw-away git repository in ``tmp_path``, plant
credential-shaped strings in known files, and exercise
``scripts/audit_git_history_for_secrets.sh`` from outside the project's own
working tree. We never reach into the project's real history here — that
would conflate "did the script fire?" with "is the project history clean?".

The synthetic secret values used below are documented decoys and are
deliberately kept outside any allowlisted directory so that the audit's
provider-prefix regexes are the *only* reason any given test passes or
fails. Do not move them under ``tests/`` inside the synthetic repo; the
script allowlists that prefix by design.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_git_history_for_secrets.sh"

# Synthetic decoys — long enough to clear the audit's minimum-length gates
# but obviously not real credentials. Each one matches exactly one provider
# alternation in the script's PATTERN.
_CEREBRAS_DECOY = "csk-AUDITSCRIPTTESTDECOYAAAAAAAAAAAAAAAAAA"
_DEEPINFRA_DECOY = "lwAUDITSCRIPTTESTDECOYAAAAAAAAAAAAAAAAAAAAAAAAA"
_OPENAI_DECOY = "sk-AUDITSCRIPTTESTDECOYBBBBBBBBBBBBBBBBBBBBBBBB"
_ANTHROPIC_DECOY = "sk-ant-AUDITSCRIPTTESTDECOYCCCCCCCCCCCCCCCCCC"


def _run_git(args: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Audit Script Test",
            "GIT_AUTHOR_EMAIL": "audit@padrino.test",
            "GIT_COMMITTER_NAME": "Audit Script Test",
            "GIT_COMMITTER_EMAIL": "audit@padrino.test",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q", "-b", "main"], cwd=path)


def _commit(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _run_audit(repo: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=repo,
        capture_output=True,
        check=False,
    )


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake_repo"
    _init_repo(repo)
    return repo


def test_audit_script_exists_and_is_executable() -> None:
    assert _SCRIPT.exists(), f"{_SCRIPT} is missing"
    assert os.access(_SCRIPT, os.X_OK), f"{_SCRIPT} must be chmod +x"


def test_cerebras_key_in_head_fails_audit(fake_repo: Path) -> None:
    target = fake_repo / "config" / "secret.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'CEREBRAS_API_KEY = "{_CEREBRAS_DECOY}"\n',
        encoding="utf-8",
    )
    _commit(fake_repo, "leak: planted Cerebras key for audit test")

    result = _run_audit(fake_repo)

    assert result.returncode == 1, (
        "audit must fail when a Cerebras-shaped key is anywhere in history; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    # Stdout shows the file path and SHA but NEVER the secret value.
    stdout = result.stdout.decode("utf-8")
    assert "config/secret.py" in stdout
    assert _CEREBRAS_DECOY not in stdout, "audit script leaked the matched secret value to stdout"
    # The full matched line lives in the gitignored audit log.
    audit_log = fake_repo / ".padrino_audit" / "audit.log"
    assert audit_log.exists()
    assert _CEREBRAS_DECOY in audit_log.read_text(encoding="utf-8")


def test_cerebras_key_in_env_example_passes_audit(fake_repo: Path) -> None:
    # `.env.example` is documented as a template. A Cerebras-shaped key in
    # that file is treated as a placeholder by convention and must not fail
    # the audit. This guards against the audit becoming a maintenance tax on
    # the credential rotation flow itself.
    (fake_repo / ".env.example").write_text(
        f"CEREBRAS_API_KEY={_CEREBRAS_DECOY}\n",
        encoding="utf-8",
    )
    _commit(fake_repo, "docs: example env file")

    result = _run_audit(fake_repo)

    assert result.returncode == 0, (
        "audit must allowlist `.env.example`; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_secret_value_never_appears_on_stdout(fake_repo: Path) -> None:
    # Plant one of every supported provider shape. None of them must reach
    # stdout; all of them must drive the exit code to 1.
    target = fake_repo / "src" / "leak.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                f'CEREBRAS = "{_CEREBRAS_DECOY}"',
                f'DEEPINFRA = "{_DEEPINFRA_DECOY}"',
                f'OPENAI = "{_OPENAI_DECOY}"',
                f'ANTHROPIC = "{_ANTHROPIC_DECOY}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _commit(fake_repo, "leak: planted multi-provider keys for audit test")

    result = _run_audit(fake_repo)

    assert result.returncode == 1
    stdout = result.stdout.decode("utf-8")
    for decoy in (_CEREBRAS_DECOY, _DEEPINFRA_DECOY, _OPENAI_DECOY, _ANTHROPIC_DECOY):
        assert decoy not in stdout, (
            f"audit script leaked {decoy!r} to stdout — stdout must only "
            "carry `<sha>\\t<file-path>` pairs"
        )


def test_populated_api_key_assignment_outside_env_example_fails(fake_repo: Path) -> None:
    target = fake_repo / "infra" / "deploy.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    populated = "AAAAAAAAAAAAAAAAAAAA" * 2  # 40 alnum chars > 20-char minimum
    target.write_text(
        f"export OPENAI_API_KEY={populated}\n",
        encoding="utf-8",
    )
    _commit(fake_repo, "infra: deploy script")

    result = _run_audit(fake_repo)

    assert result.returncode == 1
    assert populated not in result.stdout.decode("utf-8")


def test_empty_env_example_assignment_passes(fake_repo: Path) -> None:
    # `OPENAI_API_KEY=` with no value is the `.env.example`-shaped template
    # we ship; even if it lands outside `.env.example` it must not match the
    # populated-value pattern.
    target = fake_repo / "config" / "settings.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("OPENAI_API_KEY=\nDEEPINFRA_API_KEY=\n", encoding="utf-8")
    _commit(fake_repo, "settings: blank env keys")

    result = _run_audit(fake_repo)

    assert result.returncode == 0, (
        "audit must not flag empty `_API_KEY=` lines; "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_secret_only_on_non_head_ref_still_fails(fake_repo: Path) -> None:
    # An attacker who soft-deletes a leaked file in a later commit leaves
    # the secret reachable from the dangling blob; the audit must walk all
    # refs and reject the repo until the history is rewritten or rotated.
    target = fake_repo / "config" / "leaked.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'CEREBRAS_API_KEY = "{_CEREBRAS_DECOY}"\n',
        encoding="utf-8",
    )
    _commit(fake_repo, "leak: planted key")

    target.unlink()
    _commit(fake_repo, "cleanup: removed the file (but not the history)")

    result = _run_audit(fake_repo)
    assert result.returncode == 1, (
        "audit must scan all reachable history, not just HEAD; "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_audit_skips_when_not_in_git_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=not_a_repo,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2, (
        f"audit must exit 2 (env error) outside a git working tree; got {result.returncode}"
    )


def test_help_flag_does_not_run_scan(tmp_path: Path) -> None:
    # Even outside a repo, --help must succeed — operators run it to find
    # the script before bootstrapping into the project.
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--help"],
        cwd=not_a_repo,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert b"audit_git_history_for_secrets.sh" in result.stdout


def test_bash_is_available_on_path() -> None:
    # The script invokes itself via `bash`; on macOS / Linux CI workers
    # this is guaranteed, but we surface a clear error if a future agent
    # runs the suite in a stripped-down container without bash.
    assert shutil.which("bash") is not None, "bash is required to run the audit script"
