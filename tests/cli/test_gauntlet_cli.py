"""CLI tests for ``padrino gauntlet run`` roster parsing (US-084).

These exercise the YAML-parse / validation path without reaching a provider:
a malformed roster fails before any game is scheduled.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from padrino.cli import app

runner = CliRunner()


def test_gauntlet_run_rejects_non_mapping_roster(tmp_path: Path) -> None:
    roster = tmp_path / "roster.yaml"
    roster.write_text("roster: [not, a, mapping]\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "gauntlet",
            "run",
            "--roster",
            str(roster),
            "--league-id",
            "00000000-0000-0000-0000-000000000000",
            "--db-url",
            f"sqlite+aiosqlite:///{tmp_path / 'x.sqlite'}",
        ],
    )
    assert result.exit_code != 0
    assert "roster" in result.output.lower()


def test_gauntlet_run_rejects_non_uuid_build_ids(tmp_path: Path) -> None:
    roster = tmp_path / "roster.yaml"
    roster.write_text("roster:\n  P01: not-a-uuid\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "gauntlet",
            "run",
            "--roster",
            str(roster),
            "--league-id",
            "00000000-0000-0000-0000-000000000000",
            "--db-url",
            f"sqlite+aiosqlite:///{tmp_path / 'x.sqlite'}",
        ],
    )
    assert result.exit_code != 0
    assert "uuid" in result.output.lower()
