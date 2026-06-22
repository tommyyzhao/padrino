"""Padrino ruleset modules and the dynamic resolver.

`Ruleset` is the canonical structural contract a ruleset module must satisfy.
Engine submodules (``role_assignment``, ``win_conditions``, ``phases``,
``observations``) each declare their own *narrow* Protocol covering only the
fields they consume — that interface segregation lets unit tests drive them
with minimal stubs. This module's `Ruleset` is the *full* contract returned at
the resolver boundary, where callers may touch any part of the surface.
"""

from __future__ import annotations

from typing import Protocol

from padrino.core.engine.win_conditions import WinCondition
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily

BUILTIN_RULESET_IDS: tuple[str, ...] = (
    "mini7_v1",
    "bench10_v1",
    "roleblock10_v1",
    "deception13_v1",
    "sk12_v1",
    "jester8_v1",
)


class Ruleset(Protocol):
    """Full structural contract for a ruleset module (e.g. ``mini7_v1``)."""

    RULESET_ID: str
    RATING_CONTEXT_KIND: RatingContextKind
    IS_CANONICAL: bool
    RATING_CONTEXT_DISPLAY_LABEL: str
    PLAYER_COUNT: int
    ROLE_COUNTS: dict[Role, int]
    ROLE_FACTIONS: dict[Role, Faction]
    WIN_CONDITIONS: tuple[WinCondition, ...]
    ALT_WIN_CONDITIONS: tuple[str, ...]
    SOLO_FACTIONS: tuple[str, ...]
    FACTION_MUTATION_ALLOWED: bool
    KINGMAKING_OBJECTIVE: bool
    DISCUSSION_ROUNDS_PER_DAY: int
    MAX_DAYS: int
    PUBLIC_MESSAGE_MAX_CHARS: int
    PRIVATE_MESSAGE_MAX_CHARS: int
    MEMORY_UPDATE_MAX_CHARS: int
    PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT: int
    LLM_TIMEOUT_SECONDS: int
    TEMPERATURE: float
    TOP_P: float

    def role_family_for(self, role: Role) -> RoleFamily: ...
    def faction_for(self, role: Role) -> Faction: ...


def get_ruleset(ruleset_id: str) -> Ruleset:
    """Resolve and return a ruleset module by its string identifier."""
    if ruleset_id == "mini7_v1":
        from padrino.core.rulesets import mini7_v1

        return mini7_v1
    elif ruleset_id == "bench10_v1":
        from padrino.core.rulesets import bench10_v1

        return bench10_v1
    elif ruleset_id == "roleblock10_v1":
        from padrino.core.rulesets import roleblock10_v1

        return roleblock10_v1
    elif ruleset_id == "deception13_v1":
        from padrino.core.rulesets import deception13_v1

        return deception13_v1
    elif ruleset_id == "sk12_v1":
        from padrino.core.rulesets import sk12_v1

        return sk12_v1
    elif ruleset_id == "jester8_v1":
        from padrino.core.rulesets import jester8_v1

        return jester8_v1
    else:
        raise ValueError(f"Unknown ruleset_id: {ruleset_id!r}")
