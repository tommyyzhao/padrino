"""deception13_v1 ruleset constants and helpers.

Ruleset: 13 players, 1 Godfather, 1 Mafia Roleblocker, 1 Janitor,
1 Mafia Goon, 1 Detective, 1 Doctor, 7 Villagers. MAX_DAYS=5.
"""

from __future__ import annotations

from typing import Final

from padrino.core.engine.win_conditions import WinCondition, canonical_two_faction_win_conditions
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily

RULESET_ID: Final[str] = "deception13_v1"
RATING_CONTEXT_KIND: Final[RatingContextKind] = RatingContextKind.CANONICAL_TEAM
IS_CANONICAL: Final[bool] = True
RATING_CONTEXT_DISPLAY_LABEL: Final[str] = "Deception 13 canonical team"
PLAYER_COUNT: Final[int] = 13

ROLE_COUNTS: Final[dict[Role, int]] = {
    Role.GODFATHER: 1,
    Role.MAFIA_ROLEBLOCKER: 1,
    Role.JANITOR: 1,
    Role.MAFIA_GOON: 1,
    Role.DETECTIVE: 1,
    Role.DOCTOR: 1,
    Role.VILLAGER: 7,
}

DISCUSSION_ROUNDS_PER_DAY: Final[int] = 3
MAX_DAYS: Final[int] = 5
PUBLIC_MESSAGE_MAX_CHARS: Final[int] = 600
PRIVATE_MESSAGE_MAX_CHARS: Final[int] = 600
MEMORY_UPDATE_MAX_CHARS: Final[int] = 1200
PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT: Final[int] = 80
LLM_TIMEOUT_SECONDS: Final[int] = 45
TEMPERATURE: Final[float] = 0.7
TOP_P: Final[float] = 1.0

_ROLE_FAMILY: Final[dict[Role, RoleFamily]] = {
    Role.GODFATHER: RoleFamily.DECEPTIVE,
    Role.MAFIA_ROLEBLOCKER: RoleFamily.DECEPTIVE,
    Role.JANITOR: RoleFamily.DECEPTIVE,
    Role.MAFIA_GOON: RoleFamily.DECEPTIVE,
    Role.DETECTIVE: RoleFamily.INVESTIGATIVE,
    Role.DOCTOR: RoleFamily.PROTECTIVE,
    Role.VILLAGER: RoleFamily.VANILLA_TOWN,
}

ROLE_FACTIONS: Final[dict[Role, Faction]] = {
    Role.GODFATHER: Faction.MAFIA,
    Role.MAFIA_ROLEBLOCKER: Faction.MAFIA,
    Role.JANITOR: Faction.MAFIA,
    Role.MAFIA_GOON: Faction.MAFIA,
    Role.DETECTIVE: Faction.TOWN,
    Role.DOCTOR: Faction.TOWN,
    Role.VILLAGER: Faction.TOWN,
}
WIN_CONDITIONS: Final[tuple[WinCondition, ...]] = canonical_two_faction_win_conditions()
ALT_WIN_CONDITIONS: Final[tuple[str, ...]] = ()
SOLO_FACTIONS: Final[tuple[str, ...]] = ()
FACTION_MUTATION_ALLOWED: Final[bool] = False
KINGMAKING_OBJECTIVE: Final[bool] = False


def role_family_for(role: Role) -> RoleFamily:
    """Return the RoleFamily for a given Role."""
    return _ROLE_FAMILY[role]


def faction_for(role: Role) -> Faction:
    """Return the Faction for a given Role."""
    return ROLE_FACTIONS[role]
