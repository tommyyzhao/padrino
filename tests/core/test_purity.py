"""Pure-core firewall check.

Walk every module under ``src/padrino/core/`` with ``ast`` and assert that it
does NOT import any module on the forbidden list. This enforces the rule
spelled out in ``AGENTS.md`` and ``CLAUDE.md``: the core engine must remain
deterministic, dependency-free, and side-effect-free.

If a future change needs an exception (e.g. ``datetime.timezone`` for a type
hint only), update this test deliberately rather than removing it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[2] / "src" / "padrino" / "core"

FORBIDDEN_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "random",
        "secrets",
        "asyncio",
        "datetime",
        "time",
        "sqlalchemy",
        "litellm",
        "httpx",
    }
)

FORBIDDEN_PADRINO_PREFIXES: tuple[str, ...] = (
    "padrino.db",
    "padrino.llm",
    "padrino.api",
    "padrino.runner",
)

# Deliberate, narrow exceptions (see module docstring). Keyed by core-relative
# POSIX path -> the otherwise-forbidden top-level modules that module may import.
# ``core/scheduling`` (US-085) needs ``datetime`` for pure cron *arithmetic*:
# the reference moment is injected, never read from the wall clock. The
# ``test_allowed_datetime_modules_make_no_wallclock_calls`` test below enforces
# that this exception cannot smuggle in an actual wall-clock read.
# ``core/disconnect`` (US-150) needs ``datetime`` for pure grace-window
# *arithmetic*: the reference ``now`` is injected, never read from the clock.
ALLOWED_FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    "scheduling/__init__.py": frozenset({"datetime"}),
    "disconnect.py": frozenset({"datetime"}),
}

# Attribute names that read the wall clock — banned even in allowlisted modules.
WALLCLOCK_ATTRS: frozenset[str] = frozenset({"now", "utcnow", "today"})


def _iter_core_modules() -> list[Path]:
    return sorted(CORE_ROOT.rglob("*.py"))


def _allowed_for(path: Path) -> frozenset[str]:
    rel = path.relative_to(CORE_ROOT).as_posix()
    return ALLOWED_FORBIDDEN_IMPORTS.get(rel, frozenset())


def _top_level(name: str) -> str:
    return name.split(".", 1)[0]


def _is_forbidden(module_name: str) -> bool:
    if _top_level(module_name) in FORBIDDEN_TOP_LEVEL:
        return True
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in FORBIDDEN_PADRINO_PREFIXES
    )


@pytest.mark.parametrize(
    "path",
    _iter_core_modules(),
    ids=lambda p: str(p.relative_to(CORE_ROOT.parents[2])),
)
def test_core_module_has_no_forbidden_imports(path: Path) -> None:
    allowed = _allowed_for(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name) and _top_level(alias.name) not in allowed:
                    offenders.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and module and _is_forbidden(module) and module not in allowed:
                offenders.append((node.lineno, module))
    assert not offenders, (
        f"{path.relative_to(CORE_ROOT.parents[2])} imports forbidden modules: {offenders}"
    )


@pytest.mark.parametrize(
    "path",
    [CORE_ROOT / rel for rel in ALLOWED_FORBIDDEN_IMPORTS],
    ids=list(ALLOWED_FORBIDDEN_IMPORTS),
)
def test_allowed_datetime_modules_make_no_wallclock_calls(path: Path) -> None:
    """A module allowed to import ``datetime`` must still never read the clock.

    Catches ``datetime.now(...)`` / ``.utcnow()`` / ``.today()`` so the narrow
    import exception cannot become a wall-clock backdoor into pure-core.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WALLCLOCK_ATTRS
        ):
            offenders.append((node.lineno, node.func.attr))
    assert not offenders, (
        f"{path.relative_to(CORE_ROOT.parents[2])} makes wall-clock calls: {offenders}"
    )


def test_core_root_exists() -> None:
    assert CORE_ROOT.is_dir(), f"expected {CORE_ROOT} to exist"
    assert _iter_core_modules(), "no core modules discovered — check CORE_ROOT path"
