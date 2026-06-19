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


class LeagueKind(StrEnum):
    """Discriminator separating the scientific benchmark from the human lane.

    A ``SCIENTIFIC`` league owns the sacred ``Rating`` / ``RatingEvent`` tables.
    A ``HUMANS_INCLUDED`` league is the dormant, casual, humans-included league
    (Wave 9): its games write ZERO scientific rating rows and its ELO lives in
    the sibling ``human_rating`` / ``human_rating_event`` tables (not written in
    v1). ``SCIENTIFIC`` is the legacy default so existing leagues are unchanged.
    """

    SCIENTIFIC = "SCIENTIFIC"
    HUMANS_INCLUDED = "HUMANS_INCLUDED"


class IdentityMode(StrEnum):
    """Per-game disclosure mode for human-vs-AI / model identity (Wave 9).

    ``ANONYMOUS`` is the default and the fail-closed value: no live / observation
    / spectator surface reveals which seats are human vs AI, nor model/provider
    identity, before the endgame reveal. ``TRANSPARENT`` opts a game out of
    stripping. The mode is frozen after game start. The pure fail-closed
    coercion chokepoint lives in ``core.observation_privacy.coerce_identity_mode``
    (which deliberately stays string-based so it has no dependency on this enum).
    """

    ANONYMOUS = "ANONYMOUS"
    TRANSPARENT = "TRANSPARENT"


class PhaseKind(StrEnum):
    """High-level phase type within a game."""

    SETUP = "SETUP"
    NIGHT_0_MAFIA_INTRO = "NIGHT_0_MAFIA_INTRO"
    DAY_DISCUSSION = "DAY_DISCUSSION"
    DAY_VOTE = "DAY_VOTE"
    NIGHT_MAFIA_DISCUSSION = "NIGHT_MAFIA_DISCUSSION"
    NIGHT_ACTIONS = "NIGHT_ACTIONS"
    TERMINAL = "TERMINAL"
