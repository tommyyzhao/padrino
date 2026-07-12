"""Immutable game-rules snapshots for built-in and custom rulesets."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Annotated, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.win_conditions import WinCondition, WinConditionKind
from padrino.core.enums import ActionType, Faction, RatingContextKind, Role, RoleFamily

BUILTIN_CADENCE_ID: Final[str] = "builtin_sync_v1"
SUPPORTED_CADENCE_IDS: Final[frozenset[str]] = frozenset({BUILTIN_CADENCE_ID})
DRAW_WINNER: Final[str] = "DRAW"

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class _RulesetLike(Protocol):
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


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RoleCountSnapshot(_FrozenModel):
    """One role and its count in the starting role multiset."""

    role: Role
    count: PositiveInt


class RoleFactionSnapshot(_FrozenModel):
    """Faction alignment for a role present in the snapshot."""

    role: Role
    faction: Faction


class RoleFamilySnapshot(_FrozenModel):
    """Role-family metadata for a role present in the snapshot."""

    role: Role
    role_family: RoleFamily


class RoleNightActionSnapshot(_FrozenModel):
    """Active night action attached to one role."""

    role: Role
    action_type: ActionType


class MessageLimitSnapshot(_FrozenModel):
    """Integer-only observation and transcript limits."""

    public_message_max_chars: PositiveInt
    private_message_max_chars: PositiveInt
    memory_update_max_chars: PositiveInt
    public_transcript_recent_message_limit: PositiveInt


class AgentResponsePolicySnapshot(_FrozenModel):
    """Default response policy values historically stored on ruleset modules."""

    llm_timeout_seconds: PositiveInt
    temperature: StrictStr
    top_p: StrictStr

    @field_validator("temperature", "top_p")
    @classmethod
    def _validate_decimal_string(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("numeric policy strings must be non-empty and trimmed")
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError("numeric policy strings must parse as finite decimals") from exc
        if parsed != parsed or parsed in {float("inf"), float("-inf")}:
            raise ValueError("numeric policy strings must parse as finite decimals")
        return value


class WinConditionSnapshot(_FrozenModel):
    """Serializable form of one ordered terminal-condition declaration."""

    kind: WinConditionKind
    winner: NonEmptyStr
    reason: NonEmptyStr
    faction: Faction | None = None
    target_factions: tuple[Faction, ...] = ()
    opponent_factions: tuple[Faction, ...] = ()
    blocked_by_alive_factions: tuple[Faction, ...] = ()
    trigger: StrictStr | None = None

    @field_validator("target_factions", "opponent_factions", "blocked_by_alive_factions")
    @classmethod
    def _sort_factions(cls, value: tuple[Faction, ...]) -> tuple[Faction, ...]:
        return tuple(sorted(value, key=lambda faction: faction.value))

    @field_validator("trigger")
    @classmethod
    def _validate_trigger(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("trigger must be non-empty and trimmed")
        return value

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> WinConditionSnapshot:
        if self.kind is WinConditionKind.TARGET_FACTIONS_ELIMINATED and not self.target_factions:
            raise ValueError("TARGET_FACTIONS_ELIMINATED requires target_factions")
        if self.kind is WinConditionKind.FACTION_PARITY and (
            self.faction is None or not self.opponent_factions
        ):
            raise ValueError("FACTION_PARITY requires faction and opponent_factions")
        if self.kind is WinConditionKind.SOLO_LAST_ALIVE and self.faction is None:
            raise ValueError("SOLO_LAST_ALIVE requires faction")
        if self.kind is WinConditionKind.ALT_TRIGGER and self.trigger is None:
            raise ValueError("ALT_TRIGGER requires trigger")
        return self

    @classmethod
    def from_win_condition(cls, condition: WinCondition) -> WinConditionSnapshot:
        """Build a snapshot row from the engine win-condition model."""
        return cls(
            kind=condition.kind,
            winner=condition.winner,
            reason=condition.reason,
            faction=condition.faction,
            target_factions=condition.target_factions,
            opponent_factions=condition.opponent_factions,
            blocked_by_alive_factions=condition.blocked_by_alive_factions,
            trigger=condition.trigger,
        )

    def to_win_condition(self) -> WinCondition:
        """Convert back to the engine win-condition model."""
        return WinCondition(
            kind=self.kind,
            winner=self.winner,
            reason=self.reason,
            faction=self.faction,
            target_factions=self.target_factions,
            opponent_factions=self.opponent_factions,
            blocked_by_alive_factions=self.blocked_by_alive_factions,
            trigger=self.trigger,
        )

    def to_canonical_dict(self) -> dict[str, object]:
        """Return a JSON-native canonical dict for this condition."""
        return {
            "blocked_by_alive_factions": [
                faction.value for faction in self.blocked_by_alive_factions
            ],
            "faction": None if self.faction is None else self.faction.value,
            "kind": self.kind.value,
            "opponent_factions": [faction.value for faction in self.opponent_factions],
            "reason": self.reason,
            "target_factions": [faction.value for faction in self.target_factions],
            "trigger": self.trigger,
            "winner": self.winner,
        }


class GameRulesSnapshot(_FrozenModel):
    """Frozen, canonical rules value used to hash-address game mechanics."""

    ruleset_id: StrictStr | None = None
    rating_context_kind: RatingContextKind
    is_canonical: bool
    rating_context_display_label: NonEmptyStr
    player_count: PositiveInt
    role_counts: tuple[RoleCountSnapshot, ...]
    role_factions: tuple[RoleFactionSnapshot, ...]
    role_families: tuple[RoleFamilySnapshot, ...]
    role_night_actions: tuple[RoleNightActionSnapshot, ...] = ()
    win_conditions: tuple[WinConditionSnapshot, ...]
    alt_win_conditions: tuple[StrictStr, ...] = ()
    solo_factions: tuple[Faction, ...] = ()
    faction_mutation_allowed: bool = False
    kingmaking_objective: bool = False
    discussion_rounds_per_day: PositiveInt
    max_days: PositiveInt
    message_limits: MessageLimitSnapshot
    cadence_id: StrictStr
    agent_response_policy: AgentResponsePolicySnapshot

    @field_validator("ruleset_id")
    @classmethod
    def _validate_ruleset_id(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("ruleset_id must be non-empty and trimmed")
        return value

    @field_validator("cadence_id")
    @classmethod
    def _validate_cadence_id(cls, value: str) -> str:
        if value not in SUPPORTED_CADENCE_IDS:
            raise ValueError(f"unknown cadence id: {value}")
        return value

    @field_validator("alt_win_conditions")
    @classmethod
    def _validate_alt_win_conditions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not condition or condition.strip() != condition for condition in value):
            raise ValueError("alt win-condition identifiers must be non-empty and trimmed")
        if len(set(value)) != len(value):
            raise ValueError("alt win-condition identifiers must be unique")
        return value

    @field_validator("solo_factions")
    @classmethod
    def _sort_solo_factions(cls, value: tuple[Faction, ...]) -> tuple[Faction, ...]:
        if len(set(value)) != len(value):
            raise ValueError("solo factions must be unique")
        return tuple(sorted(value, key=lambda faction: faction.value))

    @model_validator(mode="after")
    def _validate_snapshot(self) -> GameRulesSnapshot:
        role_count_roles = _unique_roles(
            (entry.role for entry in self.role_counts),
            "role_counts",
        )
        if not role_count_roles:
            raise ValueError("role_counts must not be empty")
        total_roles = sum(entry.count for entry in self.role_counts)
        if total_roles != self.player_count:
            raise ValueError(
                f"role count total {total_roles} does not match player_count {self.player_count}"
            )

        faction_roles = _unique_roles(
            (entry.role for entry in self.role_factions),
            "role_factions",
        )
        family_roles = _unique_roles(
            (entry.role for entry in self.role_families),
            "role_families",
        )
        if faction_roles != role_count_roles:
            raise ValueError("role_factions must cover exactly the role_counts roles")
        if family_roles != role_count_roles:
            raise ValueError("role_families must cover exactly the role_counts roles")

        night_action_roles = _unique_roles(
            (entry.role for entry in self.role_night_actions),
            "role_night_actions",
        )
        if not night_action_roles.issubset(role_count_roles):
            raise ValueError("role_night_actions must be a subset of role_counts roles")

        if not self.win_conditions:
            raise ValueError("win_conditions must not be empty")
        _validate_winners(self.win_conditions)
        _validate_alt_triggers(self.alt_win_conditions, self.win_conditions)
        return self

    @property
    def RULESET_ID(self) -> str:
        return self.ruleset_id or custom_ruleset_id(self)

    @property
    def RATING_CONTEXT_KIND(self) -> RatingContextKind:
        return self.rating_context_kind

    @property
    def IS_CANONICAL(self) -> bool:
        return self.is_canonical

    @property
    def RATING_CONTEXT_DISPLAY_LABEL(self) -> str:
        return self.rating_context_display_label

    @property
    def PLAYER_COUNT(self) -> int:
        return self.player_count

    @property
    def ROLE_COUNTS(self) -> dict[Role, int]:
        return {entry.role: entry.count for entry in self.role_counts}

    @property
    def ROLE_FACTIONS(self) -> dict[Role, Faction]:
        return {entry.role: entry.faction for entry in self.role_factions}

    @property
    def WIN_CONDITIONS(self) -> tuple[WinCondition, ...]:
        return tuple(condition.to_win_condition() for condition in self.win_conditions)

    @property
    def ALT_WIN_CONDITIONS(self) -> tuple[str, ...]:
        return self.alt_win_conditions

    @property
    def SOLO_FACTIONS(self) -> tuple[str, ...]:
        return tuple(faction.value for faction in self.solo_factions)

    @property
    def FACTION_MUTATION_ALLOWED(self) -> bool:
        return self.faction_mutation_allowed

    @property
    def KINGMAKING_OBJECTIVE(self) -> bool:
        return self.kingmaking_objective

    @property
    def DISCUSSION_ROUNDS_PER_DAY(self) -> int:
        return self.discussion_rounds_per_day

    @property
    def MAX_DAYS(self) -> int:
        return self.max_days

    @property
    def PUBLIC_MESSAGE_MAX_CHARS(self) -> int:
        return self.message_limits.public_message_max_chars

    @property
    def PRIVATE_MESSAGE_MAX_CHARS(self) -> int:
        return self.message_limits.private_message_max_chars

    @property
    def MEMORY_UPDATE_MAX_CHARS(self) -> int:
        return self.message_limits.memory_update_max_chars

    @property
    def PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT(self) -> int:
        return self.message_limits.public_transcript_recent_message_limit

    @property
    def LLM_TIMEOUT_SECONDS(self) -> int:
        return self.agent_response_policy.llm_timeout_seconds

    @property
    def TEMPERATURE(self) -> float:
        return float(self.agent_response_policy.temperature)

    @property
    def TOP_P(self) -> float:
        return float(self.agent_response_policy.top_p)

    def role_family_for(self, role: Role) -> RoleFamily:
        """Return the role family for ``role``."""
        return {entry.role: entry.role_family for entry in self.role_families}[role]

    def faction_for(self, role: Role) -> Faction:
        """Return the faction for ``role``."""
        return self.ROLE_FACTIONS[role]


_ROLE_NIGHT_ACTIONS: Final[dict[Role, ActionType]] = {
    Role.MAFIA_GOON: ActionType.MAFIA_KILL,
    Role.NINJA: ActionType.MAFIA_KILL,
    Role.GODFATHER: ActionType.MAFIA_KILL,
    Role.MAFIA_ROLEBLOCKER: ActionType.ROLEBLOCK,
    Role.FRAMER: ActionType.FRAME,
    Role.JANITOR: ActionType.CLEAN,
    Role.DETECTIVE: ActionType.INVESTIGATE,
    Role.DOCTOR: ActionType.PROTECT,
    Role.TRACKER: ActionType.TRACK,
    Role.WATCHER: ActionType.WATCH,
    Role.SERIAL_KILLER: ActionType.SERIAL_KILL,
}


def snapshot_from_ruleset(
    ruleset: _RulesetLike,
    *,
    cadence_id: str = BUILTIN_CADENCE_ID,
) -> GameRulesSnapshot:
    """Project a registered ruleset module into a frozen rules snapshot."""
    role_counts = tuple(
        RoleCountSnapshot(role=role, count=count) for role, count in ruleset.ROLE_COUNTS.items()
    )
    role_factions = tuple(
        RoleFactionSnapshot(role=role, faction=ruleset.ROLE_FACTIONS[role])
        for role in ruleset.ROLE_COUNTS
    )
    role_families = tuple(
        RoleFamilySnapshot(role=role, role_family=ruleset.role_family_for(role))
        for role in ruleset.ROLE_COUNTS
    )
    role_night_actions = tuple(
        RoleNightActionSnapshot(role=role, action_type=_ROLE_NIGHT_ACTIONS[role])
        for role in ruleset.ROLE_COUNTS
        if role in _ROLE_NIGHT_ACTIONS
    )
    return GameRulesSnapshot(
        ruleset_id=ruleset.RULESET_ID,
        rating_context_kind=ruleset.RATING_CONTEXT_KIND,
        is_canonical=ruleset.IS_CANONICAL,
        rating_context_display_label=ruleset.RATING_CONTEXT_DISPLAY_LABEL,
        player_count=ruleset.PLAYER_COUNT,
        role_counts=role_counts,
        role_factions=role_factions,
        role_families=role_families,
        role_night_actions=role_night_actions,
        win_conditions=tuple(
            WinConditionSnapshot.from_win_condition(condition)
            for condition in ruleset.WIN_CONDITIONS
        ),
        alt_win_conditions=ruleset.ALT_WIN_CONDITIONS,
        solo_factions=tuple(Faction(faction) for faction in ruleset.SOLO_FACTIONS),
        faction_mutation_allowed=ruleset.FACTION_MUTATION_ALLOWED,
        kingmaking_objective=ruleset.KINGMAKING_OBJECTIVE,
        discussion_rounds_per_day=ruleset.DISCUSSION_ROUNDS_PER_DAY,
        max_days=ruleset.MAX_DAYS,
        message_limits=MessageLimitSnapshot(
            public_message_max_chars=ruleset.PUBLIC_MESSAGE_MAX_CHARS,
            private_message_max_chars=ruleset.PRIVATE_MESSAGE_MAX_CHARS,
            memory_update_max_chars=ruleset.MEMORY_UPDATE_MAX_CHARS,
            public_transcript_recent_message_limit=ruleset.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT,
        ),
        cadence_id=cadence_id,
        agent_response_policy=AgentResponsePolicySnapshot(
            llm_timeout_seconds=ruleset.LLM_TIMEOUT_SECONDS,
            temperature=str(ruleset.TEMPERATURE),
            top_p=str(ruleset.TOP_P),
        ),
    )


def canonical_rules_dict(snapshot: GameRulesSnapshot) -> dict[str, object]:
    """Return the JSON-native, sorted rules representation covered by the hash."""
    return {
        "agent_response_policy": {
            "llm_timeout_seconds": snapshot.agent_response_policy.llm_timeout_seconds,
            "temperature": snapshot.agent_response_policy.temperature,
            "top_p": snapshot.agent_response_policy.top_p,
        },
        "alt_win_conditions": sorted(snapshot.alt_win_conditions),
        "cadence_id": snapshot.cadence_id,
        "discussion_rounds_per_day": snapshot.discussion_rounds_per_day,
        "faction_mutation_allowed": snapshot.faction_mutation_allowed,
        "is_canonical": snapshot.is_canonical,
        "kingmaking_objective": snapshot.kingmaking_objective,
        "max_days": snapshot.max_days,
        "message_limits": {
            "memory_update_max_chars": snapshot.message_limits.memory_update_max_chars,
            "private_message_max_chars": snapshot.message_limits.private_message_max_chars,
            "public_message_max_chars": snapshot.message_limits.public_message_max_chars,
            "public_transcript_recent_message_limit": (
                snapshot.message_limits.public_transcript_recent_message_limit
            ),
        },
        "player_count": snapshot.player_count,
        "rating_context_display_label": snapshot.rating_context_display_label,
        "rating_context_kind": snapshot.rating_context_kind.value,
        "role_counts": [
            {"count": entry.count, "role": entry.role.value}
            for entry in sorted(snapshot.role_counts, key=lambda item: item.role.value)
        ],
        "role_factions": [
            {"faction": entry.faction.value, "role": entry.role.value}
            for entry in sorted(snapshot.role_factions, key=lambda item: item.role.value)
        ],
        "role_families": [
            {"role": entry.role.value, "role_family": entry.role_family.value}
            for entry in sorted(snapshot.role_families, key=lambda item: item.role.value)
        ],
        "role_night_actions": [
            {"action_type": entry.action_type.value, "role": entry.role.value}
            for entry in sorted(snapshot.role_night_actions, key=lambda item: item.role.value)
        ],
        "solo_factions": [faction.value for faction in snapshot.solo_factions],
        "win_conditions": [condition.to_canonical_dict() for condition in snapshot.win_conditions],
    }


def canonical_rules_json(snapshot: GameRulesSnapshot) -> bytes:
    """Return sorted compact UTF-8 JSON bytes for ``snapshot``."""
    return canonical_dumps(canonical_rules_dict(snapshot))


def canonical_rules_hash(snapshot: GameRulesSnapshot) -> str:
    """Return the SHA-256 hash of ``snapshot``'s canonical JSON bytes."""
    return hashlib.sha256(canonical_rules_json(snapshot)).hexdigest()


def custom_ruleset_id(snapshot: GameRulesSnapshot) -> str:
    """Return the hash-addressed custom ruleset id for ``snapshot``."""
    return f"custom:{canonical_rules_hash(snapshot)}"


def assert_ruleset_id_matches_snapshot(
    *,
    ruleset_id: str,
    snapshot: GameRulesSnapshot,
    rules_snapshot_hash: str | None = None,
) -> None:
    """Validate that ``ruleset_id`` addresses ``snapshot``.

    Built-in snapshots carry their registered ``ruleset_id`` at runtime. Custom
    snapshots omit it and are addressed as ``custom:<sha256(canonical rules)>``.
    The optional persisted hash is checked against the same canonical body.
    """
    actual_hash = canonical_rules_hash(snapshot)
    if rules_snapshot_hash is not None and rules_snapshot_hash != actual_hash:
        raise ValueError("rules_snapshot_hash_mismatch")
    expected_ruleset_id = snapshot.ruleset_id or f"custom:{actual_hash}"
    if ruleset_id != expected_ruleset_id:
        raise ValueError(
            f"ruleset_id_snapshot_mismatch: expected {expected_ruleset_id!r}, got {ruleset_id!r}"
        )


def _unique_roles(roles: Iterable[object], field_name: str) -> frozenset[Role]:
    seen: set[Role] = set()
    for role in roles:
        if not isinstance(role, Role):
            raise ValueError(f"{field_name} contains invalid role {role!r}")
        if role in seen:
            raise ValueError(f"{field_name} contains duplicate role {role.value}")
        seen.add(role)
    return frozenset(seen)


def _validate_winners(conditions: tuple[WinConditionSnapshot, ...]) -> None:
    valid_winners = {faction.value for faction in Faction} | {DRAW_WINNER}
    for condition in conditions:
        if condition.winner not in valid_winners:
            raise ValueError(f"unknown win-condition winner: {condition.winner}")


def _validate_alt_triggers(
    alt_win_conditions: tuple[str, ...],
    win_conditions: tuple[WinConditionSnapshot, ...],
) -> None:
    triggers = frozenset(
        condition.trigger
        for condition in win_conditions
        if condition.kind is WinConditionKind.ALT_TRIGGER and condition.trigger is not None
    )
    if frozenset(alt_win_conditions) != triggers:
        raise ValueError("alt_win_conditions must match ALT_TRIGGER win-condition triggers")


__all__ = [
    "BUILTIN_CADENCE_ID",
    "DRAW_WINNER",
    "SUPPORTED_CADENCE_IDS",
    "AgentResponsePolicySnapshot",
    "GameRulesSnapshot",
    "MessageLimitSnapshot",
    "RoleCountSnapshot",
    "RoleFactionSnapshot",
    "RoleFamilySnapshot",
    "RoleNightActionSnapshot",
    "WinConditionSnapshot",
    "assert_ruleset_id_matches_snapshot",
    "canonical_rules_dict",
    "canonical_rules_hash",
    "canonical_rules_json",
    "custom_ruleset_id",
    "snapshot_from_ruleset",
]
