"""Tests for the ``padrino demo-gauntlet`` CLI command (US-046).

The mock adapter path (default) needs no API keys and must produce a fully
populated leaderboard JSON on stdout. The test invokes the typer command
against a fresh per-test SQLite file and asserts the response contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from padrino.cli import app
from padrino.core.rulesets import mini7_v1


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"


def test_demo_gauntlet_prints_leaderboard_json(tmp_path: Path) -> None:
    runner = CliRunner()
    clones = 2
    result = runner.invoke(
        app,
        [
            "demo-gauntlet",
            "--seed",
            "test-seed",
            "--clones",
            str(clones),
            "--db-url",
            _db_url(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    assert payload["ruleset_id"] == mini7_v1.RULESET_ID
    assert payload["rating_model"] == "openskill_plackett_luce_v1"
    assert payload["leaderboard_id"]
    assert payload["prompt_version"] == "demo-prompt-v1"
    assert len(payload["game_ids"]) == clones

    entries = payload["entries"]
    assert len(entries) == 1, "single agent_build means a single leaderboard entry"
    entry = entries[0]
    required = {
        "agent_build_id",
        "display_name",
        "games",
        "wins",
        "draws",
        "losses",
        "mu",
        "sigma",
        "conservative_score",
        "timeout_rate",
        "invalid_action_rate",
        "public_message_avg_chars",
        "role_family_breakdown",
        "provisional",
    }
    assert required.issubset(entry.keys())
    assert entry["display_name"] == "demo-build"
    # Every seat is the same agent_build, so games == clones * PLAYER_COUNT.
    assert entry["games"] == clones * mini7_v1.PLAYER_COUNT
    # NoopMockAdapter never submits real actions → mafia never kills, town never
    # votes, so every game runs to MAX_DAYS_REACHED DRAW.
    assert entry["draws"] == entry["games"]
    assert entry["wins"] == 0
    assert entry["losses"] == 0
    assert entry["role_family_breakdown"] == {}
    assert entry["provisional"] is True


@pytest.mark.parametrize("flag", ["--help"])
def test_demo_gauntlet_help(flag: str) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["demo-gauntlet", flag])
    assert result.exit_code == 0
    assert "demo-gauntlet" in result.stdout.lower() or "gauntlet" in result.stdout.lower()
