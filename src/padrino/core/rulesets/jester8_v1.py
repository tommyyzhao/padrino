"""jester8_v1 ruleset constants and helpers.

Ruleset: 8 players, 2 Mafia Goons, 1 Jester, 1 Detective, 1 Doctor,
3 Villagers. Non-canonical SOLO_RATE context. MAX_DAYS=5.
"""

from __future__ import annotations

from typing import Final

from padrino.core.engine.win_conditions import (
    REASON_ALL_MAFIA_ELIMINATED,
    REASON_MAX_DAYS_REACHED,
    REASON_PARITY_REACHED,
    WinCondition,
    WinConditionKind,
)
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily

RULESET_ID: Final[str] = "jester8_v1"
RATING_CONTEXT_KIND: Final[RatingContextKind] = RatingContextKind.SOLO_RATE
IS_CANONICAL: Final[bool] = False
RATING_CONTEXT_DISPLAY_LABEL: Final[str] = "Jester 8 lynch-bait"
PLAYER_COUNT: Final[int] = 8

JESTER_WINNER: Final[str] = Faction.JESTER.value
JESTER_DAY_VOTED_OUT_TRIGGER: Final[str] = "JESTER_DAY_VOTED_OUT"
REASON_JESTER_DAY_VOTED_OUT: Final[str] = "JESTER_DAY_VOTED_OUT"
JESTER_OUTCOME_LABEL: Final[str] = "JESTER_LYNCH_BAIT"

ROLE_COUNTS: Final[dict[Role, int]] = {
    Role.MAFIA_GOON: 2,
    Role.JESTER: 1,
    Role.DETECTIVE: 1,
    Role.DOCTOR: 1,
    Role.VILLAGER: 3,
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
    Role.MAFIA_GOON: RoleFamily.DECEPTIVE,
    Role.JESTER: RoleFamily.DECEPTIVE,
    Role.DETECTIVE: RoleFamily.INVESTIGATIVE,
    Role.DOCTOR: RoleFamily.PROTECTIVE,
    Role.VILLAGER: RoleFamily.VANILLA_TOWN,
}

ROLE_FACTIONS: Final[dict[Role, Faction]] = {
    Role.MAFIA_GOON: Faction.MAFIA,
    Role.JESTER: Faction.JESTER,
    Role.DETECTIVE: Faction.TOWN,
    Role.DOCTOR: Faction.TOWN,
    Role.VILLAGER: Faction.TOWN,
}

WIN_CONDITIONS: Final[tuple[WinCondition, ...]] = (
    WinCondition(
        kind=WinConditionKind.ALT_TRIGGER,
        winner=JESTER_WINNER,
        reason=REASON_JESTER_DAY_VOTED_OUT,
        trigger=JESTER_DAY_VOTED_OUT_TRIGGER,
    ),
    WinCondition(
        kind=WinConditionKind.TARGET_FACTIONS_ELIMINATED,
        winner=Faction.TOWN.value,
        reason=REASON_ALL_MAFIA_ELIMINATED,
        target_factions=(Faction.MAFIA,),
    ),
    WinCondition(
        kind=WinConditionKind.FACTION_PARITY,
        winner=Faction.MAFIA.value,
        reason=REASON_PARITY_REACHED,
        faction=Faction.MAFIA,
        opponent_factions=(Faction.TOWN,),
    ),
    WinCondition(
        kind=WinConditionKind.DAY_CAP,
        winner="DRAW",
        reason=REASON_MAX_DAYS_REACHED,
    ),
)
ALT_WIN_CONDITIONS: Final[tuple[str, ...]] = (JESTER_DAY_VOTED_OUT_TRIGGER,)
SOLO_FACTIONS: Final[tuple[str, ...]] = (Faction.JESTER.value,)
FACTION_MUTATION_ALLOWED: Final[bool] = False
KINGMAKING_OBJECTIVE: Final[bool] = False


def role_family_for(role: Role) -> RoleFamily:
    """Return the RoleFamily for a given Role."""
    return _ROLE_FAMILY[role]


def faction_for(role: Role) -> Faction:
    """Return the Faction for a given Role."""
    return ROLE_FACTIONS[role]
