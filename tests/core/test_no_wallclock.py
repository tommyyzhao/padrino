"""Wall-clock firewall check for the pure core.

The pure core must derive timestamps from injected callables, never from a
direct wall-clock read. This walks every module under ``src/padrino/core/``
with ``ast`` and asserts that none of the following call expressions appear:

    - ``datetime.utcnow(...)``
    - ``datetime.now(...)``  (and ``datetime.datetime.now(...)``)
    - ``time.time(...)``

The check is purely syntactic — it does not import the modules, so it is
robust to future refactors.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[2] / "src" / "padrino" / "core"


def _iter_core_modules() -> list[Path]:
    return sorted(CORE_ROOT.rglob("*.py"))


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return parts


FORBIDDEN_CALLS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("datetime", "utcnow"),
        ("datetime", "now"),
        ("datetime", "datetime", "utcnow"),
        ("datetime", "datetime", "now"),
        ("time", "time"),
    }
)


@pytest.mark.parametrize(
    "path",
    _iter_core_modules(),
    ids=lambda p: str(p.relative_to(CORE_ROOT.parents[2])),
)
def test_core_module_has_no_wallclock_call(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _attr_chain(node.func)
            if chain and tuple(chain) in FORBIDDEN_CALLS:
                offenders.append((node.lineno, ".".join(chain)))
    assert not offenders, (
        f"{path.relative_to(CORE_ROOT.parents[2])} calls wall-clock APIs: {offenders}"
    )
