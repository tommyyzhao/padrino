"""Domain enumerations shared across all Padrino modules."""

from __future__ import annotations

from enum import StrEnum


class Faction(StrEnum):
    """Player faction — determines win condition alignment."""

    TOWN = "TOWN"
    MAFIA = "MAFIA"
    SERIAL_KILLER = "SERIAL_KILLER"


class Role(StrEnum):
    """Specific role assigned to a seat."""

    MAFIA_GOON = "MAFIA_GOON"
    GODFATHER = "GODFATHER"
    MAFIA_ROLEBLOCKER = "MAFIA_ROLEBLOCKER"
    FRAMER = "FRAMER"
    JANITOR = "JANITOR"
    DETECTIVE = "DETECTIVE"
    DOCTOR = "DOCTOR"
    TRACKER = "TRACKER"
    WATCHER = "WATCHER"
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
    ROLEBLOCK = "ROLEBLOCK"
    FRAME = "FRAME"
    TRACK = "TRACK"
    WATCH = "WATCH"
    CLEAN = "CLEAN"


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


class RatingContextKind(StrEnum):
    """Scoring lane discriminator for a ruleset.

    ``CANONICAL_TEAM`` is the sacred two-faction OpenSkill ladder reached only
    through a SCIENTIFIC league. ``PLACEMENT`` and ``SOLO_RATE`` are sibling
    contexts that never write to ``ratings`` / ``rating_events``.
    """

    CANONICAL_TEAM = "CANONICAL_TEAM"
    PLACEMENT = "PLACEMENT"
    SOLO_RATE = "SOLO_RATE"


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


class LobbyStatus(StrEnum):
    """Lifecycle of a private friend lobby (Wave 9, US-147).

    A lobby is ``OPEN`` while members join and configure, ``LOCKED`` once the host
    locks the roster, ``LAUNCHED`` after the lobby hands off to a real game on the
    human worker lane, and ``CLOSED`` when cancelled/abandoned. ``OPEN`` is the
    create-time default.
    """

    OPEN = "OPEN"
    LOCKED = "LOCKED"
    LAUNCHED = "LAUNCHED"
    CLOSED = "CLOSED"


class LobbyStakes(StrEnum):
    """Stakes of a human-multiplayer lobby (Wave 9, US-147).

    v1 is always ``CASUAL`` (decision 10): the ELO infrastructure is
    designed-now-dormant, so a lobby's stakes are pinned to ``CASUAL`` and a
    ranked value never ships in v1.
    """

    CASUAL = "CASUAL"


class LobbySeatKind(StrEnum):
    """How an empty lobby seat will be filled at launch (Wave 9, US-147).

    A ``HUMAN`` seat is reserved for an invited member; an ``AI`` seat is filled
    at launch either by the host's pre-picked human-eligible model or by curated
    deterministic auto-fill (US-149). This is lobby-configuration data and is
    distinct from the game-time :class:`SeatKind` provenance.
    """

    HUMAN = "HUMAN"
    AI = "AI"


class FundingSource(StrEnum):
    """Who pays for a cost-tracking row's inference (Wave 9, US-151).

    ``PLATFORM`` is the byte-identical default: human play is platform-absorbed
    within a Moderate budget in v1. ``BYOK_OWNER`` (a lobby host pays with their
    own provider key) and ``SPONSOR_POOL`` (a sponsored credit pool) are designed
    now but dormant — no v1 code path writes them — so the cost-tracking schema
    is forward-compatible without a later migration.
    """

    PLATFORM = "PLATFORM"
    BYOK_OWNER = "BYOK_OWNER"
    SPONSOR_POOL = "SPONSOR_POOL"


class PhaseKind(StrEnum):
    """High-level phase type within a game."""

    SETUP = "SETUP"
    NIGHT_0_MAFIA_INTRO = "NIGHT_0_MAFIA_INTRO"
    DAY_DISCUSSION = "DAY_DISCUSSION"
    DAY_VOTE = "DAY_VOTE"
    NIGHT_MAFIA_DISCUSSION = "NIGHT_MAFIA_DISCUSSION"
    NIGHT_ACTIONS = "NIGHT_ACTIONS"
    TERMINAL = "TERMINAL"
