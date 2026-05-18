"""Release-time invariants for the packaged Padrino distribution.

These assertions belong on every default test run because they protect the
release artifact, not because they exercise behavior. If any of them fail
the published tag would ship a broken or surprising surface to consumers:

- ``padrino version`` must match the ``[project].version`` recorded in
  ``pyproject.toml`` (the source of truth) and must be readable via
  ``importlib.metadata`` — that is the seam the CLI uses at runtime.
- The ``padrino`` console-script entry point must resolve to
  ``padrino.cli:app``. Renaming or relocating the typer app silently breaks
  every downstream container image and operator runbook.
- No test module imports a private (underscore-prefixed) **submodule** of
  ``padrino`` from another package boundary. Tests inside the package
  they're exercising may import private *names*, but a cross-package
  reach into an underscore-prefixed module is a maintenance hazard and a
  signal that the import target should be promoted to public API instead.
"""

from __future__ import annotations

import ast
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from padrino.cli import app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    version = project["version"]
    assert isinstance(version, str), "pyproject.toml [project].version must be a string"
    return version


def test_padrino_version_matches_pyproject() -> None:
    expected = _pyproject_version()
    assert metadata.version("padrino") == expected

    import padrino

    assert padrino.__version__ == expected


def test_padrino_cli_version_command_prints_pyproject_version() -> None:
    expected = _pyproject_version()
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == expected


def test_padrino_console_script_entrypoint_is_stable() -> None:
    entry_points = metadata.entry_points(group="console_scripts")
    padrino_entries = [ep for ep in entry_points if ep.name == "padrino"]
    assert len(padrino_entries) == 1, (
        "Expected exactly one `padrino` console-script entry point; "
        f"found {len(padrino_entries)}: {padrino_entries}"
    )
    entry = padrino_entries[0]
    assert entry.value == "padrino.cli:app", (
        f"`padrino` entry point must remain `padrino.cli:app`; got {entry.value!r}"
    )

    loaded = entry.load()
    assert loaded is app


def _iter_test_module_files() -> list[Path]:
    return [
        path
        for path in _TESTS_ROOT.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    ]


def _is_private_submodule_path(module: str) -> bool:
    parts = module.split(".")
    return any(part.startswith("_") for part in parts[1:])


def _test_subpackage(path: Path) -> str | None:
    rel = path.relative_to(_TESTS_ROOT)
    return rel.parts[0] if len(rel.parts) > 1 else None


def _is_cross_package_private_import(
    module: str, names: list[str], test_subpkg: str | None
) -> tuple[bool, str]:
    if not module.startswith("padrino"):
        return False, ""
    module_parts = module.split(".")
    if len(module_parts) < 2:
        return False, ""
    module_subpkg = module_parts[1]
    if test_subpkg == module_subpkg:
        return False, ""
    if _is_private_submodule_path(module):
        return True, f"from {module} import {', '.join(names)}"
    for name in names:
        if not name.startswith("_") or name.startswith("__"):
            continue
        candidate = f"{module}.{name}"
        try:
            spec_path = _REPO_ROOT / "src" / Path(*candidate.split("."))
        except (TypeError, ValueError):
            continue
        if (spec_path.with_suffix(".py")).exists() or (spec_path / "__init__.py").exists():
            return True, f"from {module} import {name}"
    return False, ""


@pytest.mark.parametrize(
    "path", _iter_test_module_files(), ids=lambda p: str(p.relative_to(_TESTS_ROOT))
)
def test_no_cross_package_private_module_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    test_subpkg = _test_subpackage(path)
    offenses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            offending, msg = _is_cross_package_private_import(module, names, test_subpkg)
            if offending:
                offenses.append(f"line {node.lineno}: {msg}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("padrino"):
                    continue
                if _is_private_submodule_path(alias.name):
                    target_parts = alias.name.split(".")
                    target_subpkg = target_parts[1] if len(target_parts) >= 2 else None
                    if target_subpkg is not None and target_subpkg != test_subpkg:
                        offenses.append(f"line {node.lineno}: import {alias.name}")
    assert not offenses, (
        f"{path.relative_to(_REPO_ROOT)} reaches into a private submodule from another "
        f"package boundary: {offenses}"
    )
