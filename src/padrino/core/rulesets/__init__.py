"""Padrino ruleset modules."""

from __future__ import annotations

from typing import Any


def get_ruleset(ruleset_id: str) -> Any:
    """Resolve and return a ruleset module by its string identifier.

    Supports:
      - "mini7_v1" -> src/padrino/core/rulesets/mini7_v1.py
      - "bench10_v1" -> src/padrino/core/rulesets/bench10_v1.py
    """
    if ruleset_id == "mini7_v1":
        from padrino.core.rulesets import mini7_v1

        return mini7_v1
    elif ruleset_id == "bench10_v1":
        from padrino.core.rulesets import bench10_v1

        return bench10_v1
    else:
        raise ValueError(f"Unknown ruleset_id: {ruleset_id!r}")
