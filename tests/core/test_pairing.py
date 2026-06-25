"""Tests for the pure campaign pairing-matrix generator."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from padrino.core.rulesets import get_ruleset
from padrino.core.scheduling.pairing import (
    PairingFormat,
    analyze_pairing_matrix,
    derive_campaign_cell_seed,
    generate_pairing_matrix,
    minimum_distinct_opponents,
    mirror_leg_roster,
    required_cell_appearances,
)


def _models(count: int) -> list[str]:
    return [f"model-{i:02d}" for i in range(count)]


def test_mirror_pairing_matrix_is_ordered_and_seeded_by_campaign_cell() -> None:
    models = _models(20)

    matrix = generate_pairing_matrix(
        "wave12-campaign",
        "mini7_v1",
        models,
        format=PairingFormat.MIRROR,
        per_model_game_target=50,
    )

    ruleset = get_ruleset("mini7_v1")
    assert [cell_index for cell_index, _roster in matrix] == list(range(len(matrix)))
    assert matrix
    for cell_index, roster in matrix:
        assert len(roster) == ruleset.PLAYER_COUNT
        assert len(set(roster)) == ruleset.PLAYER_COUNT
        assert set(roster) <= set(models)
        assert (
            derive_campaign_cell_seed("wave12-campaign", cell_index)
            == hashlib.sha256(
                b"campaign:" + b"wave12-campaign" + cell_index.to_bytes(8, "big")
            ).hexdigest()
        )

    first_roster = matrix[0][1]
    assert mirror_leg_roster(first_roster, pair_leg=0) == first_roster
    assert mirror_leg_roster(first_roster, pair_leg=1) == tuple(reversed(first_roster))


@pytest.mark.parametrize(
    ("ruleset_id", "field_size", "target_games"),
    [
        ("mini7_v1", 20, 50),
        ("mini7_v1", 24, 40),
        ("bench10_v1", 20, 50),
        ("bench10_v1", 32, 40),
    ],
)
def test_pairing_matrix_guarantees_coverage_and_balanced_exposure(
    ruleset_id: str,
    field_size: int,
    target_games: int,
) -> None:
    models = _models(field_size)
    matrix = generate_pairing_matrix(
        "balanced-campaign",
        ruleset_id,
        models,
        format="MIRROR",
        per_model_game_target=target_games,
    )
    ruleset = get_ruleset(ruleset_id)
    diagnostics = analyze_pairing_matrix(
        "balanced-campaign",
        ruleset_id,
        matrix,
        format=PairingFormat.MIRROR,
        per_model_game_target=target_games,
    )

    assert len(matrix) * 20 < math.comb(field_size, ruleset.PLAYER_COUNT)
    assert diagnostics.required_cell_appearances == required_cell_appearances(
        PairingFormat.MIRROR,
        target_games,
    )
    assert diagnostics.minimum_distinct_opponents == minimum_distinct_opponents(
        field_size,
        ruleset.PLAYER_COUNT,
        diagnostics.required_cell_appearances,
    )
    assert set(diagnostics.appearances_by_model) == set(models)
    assert set(diagnostics.distinct_opponents_by_model) == set(models)
    assert all(
        appearances >= diagnostics.required_cell_appearances
        for appearances in diagnostics.appearances_by_model.values()
    )
    assert all(
        opponents >= diagnostics.minimum_distinct_opponents
        for opponents in diagnostics.distinct_opponents_by_model.values()
    )
    assert diagnostics.faction_balance_violations == ()
    assert diagnostics.role_family_balance_violations == ()


def test_pairing_matrix_is_byte_stable_across_processes() -> None:
    models = _models(21)
    matrix = generate_pairing_matrix(
        "stable-campaign",
        "mini7_v1",
        models,
        format=PairingFormat.MIRROR,
        per_model_game_target=32,
    )
    assert matrix == generate_pairing_matrix(
        "stable-campaign",
        "mini7_v1",
        models,
        format=PairingFormat.MIRROR,
        per_model_game_target=32,
    )

    script = """
import json
from padrino.core.scheduling.pairing import PairingFormat, generate_pairing_matrix
models = [f"model-{i:02d}" for i in range(21)]
matrix = generate_pairing_matrix(
    "stable-campaign",
    "mini7_v1",
    models,
    format=PairingFormat.MIRROR,
    per_model_game_target=32,
)
print(json.dumps(matrix, separators=(",", ":"), sort_keys=True))
"""
    output = subprocess.check_output([sys.executable, "-c", script], text=True)
    assert json.loads(output) == [[cell_index, list(roster)] for cell_index, roster in matrix]


def test_pairing_module_has_only_pure_imports_and_required_building_blocks() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "padrino"
        / "core"
        / "scheduling"
        / "pairing.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")

    assert "padrino.core.engine.rng" in imported_modules
    assert "padrino.core.engine.role_assignment" in imported_modules
    assert "padrino.core.rulesets" in imported_modules
    assert not {
        "random",
        "secrets",
        "datetime",
        "time",
        "sqlalchemy",
        "litellm",
        "httpx",
    }.intersection(imported_modules)
    assert not any(module.startswith("padrino.db") for module in imported_modules)
    assert not any(module.startswith("padrino.llm") for module in imported_modules)
