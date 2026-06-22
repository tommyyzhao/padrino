"""sk12_v1 ruleset constants and helpers.

Ruleset: 12 players, 3 Mafia Goons, 1 Serial Killer, 1 Detective,
1 Doctor, 6 Villagers. Non-canonical PLACEMENT context. MAX_DAYS=5.
"""

from __future__ import annotations

from typing import Final

from padrino.core.engine.win_conditions import (
    REASON_MAX_DAYS_REACHED,
    REASON_PARITY_REACHED,
    WinCondition,
    WinConditionKind,
)
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily

RULESET_ID: Final[str] = "sk12_v1"
RATING_CONTEXT_KIND: Final[RatingContextKind] = RatingContextKind.PLACEMENT
IS_CANONICAL: Final[bool] = False
RATING_CONTEXT_DISPLAY_LABEL: Final[str] = "Serial Killer 12 placement"
PLAYER_COUNT: Final[int] = 12

ROLE_COUNTS: Final[dict[Role, int]] = {
    Role.MAFIA_GOON: 3,
    Role.SERIAL_KILLER: 1,
    Role.DETECTIVE: 1,
    Role.DOCTOR: 1,
    Role.VILLAGER: 6,
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

REASON_ALL_THREATS_ELIMINATED: Final[str] = "ALL_THREATS_ELIMINATED"
REASON_SOLO_LAST_ALIVE: Final[str] = "SOLO_LAST_ALIVE"

_ROLE_FAMILY: Final[dict[Role, RoleFamily]] = {
    Role.MAFIA_GOON: RoleFamily.DECEPTIVE,
    Role.SERIAL_KILLER: RoleFamily.DECEPTIVE,
    Role.DETECTIVE: RoleFamily.INVESTIGATIVE,
    Role.DOCTOR: RoleFamily.PROTECTIVE,
    Role.VILLAGER: RoleFamily.VANILLA_TOWN,
}

ROLE_FACTIONS: Final[dict[Role, Faction]] = {
    Role.MAFIA_GOON: Faction.MAFIA,
    Role.SERIAL_KILLER: Faction.SERIAL_KILLER,
    Role.DETECTIVE: Faction.TOWN,
    Role.DOCTOR: Faction.TOWN,
    Role.VILLAGER: Faction.TOWN,
}

WIN_CONDITIONS: Final[tuple[WinCondition, ...]] = (
    WinCondition(
        kind=WinConditionKind.TARGET_FACTIONS_ELIMINATED,
        winner=Faction.TOWN.value,
        reason=REASON_ALL_THREATS_ELIMINATED,
        target_factions=(Faction.MAFIA, Faction.SERIAL_KILLER),
    ),
    WinCondition(
        kind=WinConditionKind.SOLO_LAST_ALIVE,
        winner=Faction.SERIAL_KILLER.value,
        reason=REASON_SOLO_LAST_ALIVE,
        faction=Faction.SERIAL_KILLER,
    ),
    WinCondition(
        kind=WinConditionKind.FACTION_PARITY,
        winner=Faction.MAFIA.value,
        reason=REASON_PARITY_REACHED,
        faction=Faction.MAFIA,
        opponent_factions=(Faction.TOWN,),
        blocked_by_alive_factions=(Faction.SERIAL_KILLER,),
    ),
    WinCondition(
        kind=WinConditionKind.DAY_CAP,
        winner="DRAW",
        reason=REASON_MAX_DAYS_REACHED,
    ),
)
ALT_WIN_CONDITIONS: Final[tuple[str, ...]] = ()
SOLO_FACTIONS: Final[tuple[str, ...]] = (Faction.SERIAL_KILLER.value,)
FACTION_MUTATION_ALLOWED: Final[bool] = False
KINGMAKING_OBJECTIVE: Final[bool] = False


def role_family_for(role: Role) -> RoleFamily:
    """Return the RoleFamily for a given Role."""
    return _ROLE_FAMILY[role]


def faction_for(role: Role) -> Faction:
    """Return the Faction for a given Role."""
    return ROLE_FACTIONS[role]
