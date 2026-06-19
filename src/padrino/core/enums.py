"""Domain enumerations shared across all Padrino modules."""

from __future__ import annotations

from enum import StrEnum


class Faction(StrEnum):
    """Player faction — determines win condition alignment."""

    TOWN = "TOWN"
    MAFIA = "MAFIA"


class Role(StrEnum):
    """Specific role assigned to a seat."""

    MAFIA_GOON = "MAFIA_GOON"
    DETECTIVE = "DETECTIVE"
    DOCTOR = "DOCTOR"
    VILLAGER = "VILLAGER"


class RoleFamily(StrEnum):
    """Broad category of role behaviour used for analytics and display."""

    DECEPTIVE = "DECEPTIVE"
    INVESTIGATIVE = "INVESTIGATIVE"
    PROTECTIVE = "PROTECTIVE"
    VANILLA_TOWN = "VANILLA_TOWN"


class ActionType(StrEnum):
    """Structured action a seat may submit during a phase."""

    NOOP = "NOOP"
    ABSTAIN = "ABSTAIN"
    VOTE = "VOTE"
    MAFIA_KILL = "MAFIA_KILL"
    PROTECT = "PROTECT"
    INVESTIGATE = "INVESTIGATE"


class SeatKind(StrEnum):
    """Who occupies a seat (Wave 9 human multiplayer).

    Pure data carried on ``Seat`` with NO effect on mechanics; the engine
    resolves actions identically regardless of seat kind. ``AI`` is the legacy
    default so existing event logs replay to identical state.
    """

    AI = "AI"
    HUMAN = "HUMAN"
    AI_TAKEOVER = "AI_TAKEOVER"


class PhaseKind(StrEnum):
    """High-level phase type within a game."""

    SETUP = "SETUP"
    NIGHT_0_MAFIA_INTRO = "NIGHT_0_MAFIA_INTRO"
    DAY_DISCUSSION = "DAY_DISCUSSION"
    DAY_VOTE = "DAY_VOTE"
    NIGHT_MAFIA_DISCUSSION = "NIGHT_MAFIA_DISCUSSION"
    NIGHT_ACTIONS = "NIGHT_ACTIONS"
    TERMINAL = "TERMINAL"
