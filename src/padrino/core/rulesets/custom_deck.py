"""Pure custom role-deck validation and snapshot construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, TypeAlias

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.win_conditions import (
    REASON_MAX_DAYS_REACHED,
    REASON_PARITY_REACHED,
    WinCondition,
    WinConditionKind,
    canonical_two_faction_win_conditions,
)
from padrino.core.enums import ActionType, Faction, RatingContextKind, Role, RoleFamily
from padrino.core.rulesets.framer_variance_gate import CURRENT_FRAMER_VARIANCE_GATE
from padrino.core.rulesets.mayor_variance_gate import CURRENT_MAYOR_VARIANCE_GATE
from padrino.core.rulesets.snapshot import (
    BUILTIN_CADENCE_ID,
    AgentResponsePolicySnapshot,
    GameRulesSnapshot,
    MessageLimitSnapshot,
    RoleCountSnapshot,
    RoleFactionSnapshot,
    RoleFamilySnapshot,
    RoleNightActionSnapshot,
    WinConditionSnapshot,
)

MIN_CUSTOM_PLAYER_COUNT: Final[int] = 7
MAX_CUSTOM_PLAYER_COUNT: Final[int] = 15
RoleCountsInput: TypeAlias = Mapping[Role, int] | Mapping[str, int] | Mapping[Role | str, int]

CUSTOM_DISCUSSION_ROUNDS_PER_DAY: Final[int] = 3
CUSTOM_MAX_DAYS: Final[int] = 5
CUSTOM_PUBLIC_MESSAGE_MAX_CHARS: Final[int] = 600
CUSTOM_PRIVATE_MESSAGE_MAX_CHARS: Final[int] = 600
CUSTOM_MEMORY_UPDATE_MAX_CHARS: Final[int] = 1200
CUSTOM_PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT: Final[int] = 80
CUSTOM_LLM_TIMEOUT_SECONDS: Final[int] = 45
CUSTOM_TEMPERATURE: Final[str] = "0.7"
CUSTOM_TOP_P: Final[str] = "1.0"

CUSTOM_TEAM_DISPLAY_LABEL: Final[str] = "Custom casual team"
CUSTOM_PLACEMENT_DISPLAY_LABEL: Final[str] = "Custom casual placement"
CUSTOM_SOLO_RATE_DISPLAY_LABEL: Final[str] = "Custom casual solo-rate"

JESTER_DAY_VOTED_OUT_TRIGGER: Final[str] = "JESTER_DAY_VOTED_OUT"
REASON_JESTER_DAY_VOTED_OUT: Final[str] = "JESTER_DAY_VOTED_OUT"
REASON_ALL_THREATS_ELIMINATED: Final[str] = "ALL_THREATS_ELIMINATED"
REASON_SOLO_LAST_ALIVE: Final[str] = "SOLO_LAST_ALIVE"


class CustomRoleDeckReason(StrEnum):
    """Stable machine-readable custom-deck validation reason tokens."""

    OK = "ok"
    INVALID_ROLE_COUNT = "invalid_role_count"
    INVALID_FACTION_RATIO = "invalid_faction_ratio"
    IMPOSSIBLE_WIN_CONDITIONS = "impossible_win_conditions"
    PLAYER_COUNT_MISMATCH = "player_count_mismatch"
    PLAYER_COUNT_OUT_OF_RANGE = "player_count_out_of_range"
    ROLE_COUNT_EXCEEDS_MAX = "role_count_exceeds_max"
    ROLE_GATED = "role_gated"
    ROLE_UNSUPPORTED = "role_unsupported"
    UNSUPPORTED_SOLO_ROLE_COMBINATION = "unsupported_solo_role_combination"
    ZERO_THREAT = "zero_threat"
    ZERO_TOWN = "zero_town"


class CustomRoleCapability(BaseModel):
    """UI-facing custom-role capability derived from validation definitions."""

    model_config = ConfigDict(frozen=True)

    role: Role
    faction: Faction
    role_family: RoleFamily
    available: bool
    reason: CustomRoleDeckReason | None = None
    night_action: ActionType | None = None
    max_count: int | None = None
    duplicate_allowed: bool


class CustomRoleDeckValidation(BaseModel):
    """Result of validating and normalizing a custom role deck."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reasons: tuple[CustomRoleDeckReason, ...]
    snapshot: GameRulesSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _RoleDefinition:
    role: Role
    faction: Faction
    role_family: RoleFamily
    night_action: ActionType | None = None
    available: bool = True
    unavailable_reason: CustomRoleDeckReason | None = None
    max_count: int | None = 1
    killing_threat: bool = False

    @property
    def duplicate_allowed(self) -> bool:
        return self.max_count is None


_ORDINARY_DUPLICATE_MAX: Final[None] = None

_BASE_ROLE_DEFINITIONS: Final[dict[Role, _RoleDefinition]] = {
    Role.MAFIA_GOON: _RoleDefinition(
        role=Role.MAFIA_GOON,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.MAFIA_KILL,
        max_count=_ORDINARY_DUPLICATE_MAX,
        killing_threat=True,
    ),
    Role.NINJA: _RoleDefinition(
        role=Role.NINJA,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.MAFIA_KILL,
        killing_threat=True,
    ),
    Role.GODFATHER: _RoleDefinition(
        role=Role.GODFATHER,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.MAFIA_KILL,
        killing_threat=True,
    ),
    Role.MAFIA_ROLEBLOCKER: _RoleDefinition(
        role=Role.MAFIA_ROLEBLOCKER,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.ROLEBLOCK,
    ),
    Role.FRAMER: _RoleDefinition(
        role=Role.FRAMER,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.FRAME,
        available=False,
        unavailable_reason=CustomRoleDeckReason.ROLE_GATED,
    ),
    Role.JANITOR: _RoleDefinition(
        role=Role.JANITOR,
        faction=Faction.MAFIA,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.CLEAN,
    ),
    Role.DETECTIVE: _RoleDefinition(
        role=Role.DETECTIVE,
        faction=Faction.TOWN,
        role_family=RoleFamily.INVESTIGATIVE,
        night_action=ActionType.INVESTIGATE,
    ),
    Role.DOCTOR: _RoleDefinition(
        role=Role.DOCTOR,
        faction=Faction.TOWN,
        role_family=RoleFamily.PROTECTIVE,
        night_action=ActionType.PROTECT,
    ),
    Role.TRACKER: _RoleDefinition(
        role=Role.TRACKER,
        faction=Faction.TOWN,
        role_family=RoleFamily.INVESTIGATIVE,
        night_action=ActionType.TRACK,
    ),
    Role.WATCHER: _RoleDefinition(
        role=Role.WATCHER,
        faction=Faction.TOWN,
        role_family=RoleFamily.INVESTIGATIVE,
        night_action=ActionType.WATCH,
    ),
    Role.MAYOR: _RoleDefinition(
        role=Role.MAYOR,
        faction=Faction.TOWN,
        role_family=RoleFamily.VANILLA_TOWN,
        available=False,
        unavailable_reason=CustomRoleDeckReason.ROLE_GATED,
    ),
    Role.COMMUTER: _RoleDefinition(
        role=Role.COMMUTER,
        faction=Faction.TOWN,
        role_family=RoleFamily.PROTECTIVE,
        available=False,
        unavailable_reason=CustomRoleDeckReason.ROLE_GATED,
    ),
    Role.MASON: _RoleDefinition(
        role=Role.MASON,
        faction=Faction.TOWN,
        role_family=RoleFamily.VANILLA_TOWN,
        available=False,
        unavailable_reason=CustomRoleDeckReason.ROLE_GATED,
    ),
    Role.SERIAL_KILLER: _RoleDefinition(
        role=Role.SERIAL_KILLER,
        faction=Faction.SERIAL_KILLER,
        role_family=RoleFamily.DECEPTIVE,
        night_action=ActionType.SERIAL_KILL,
        killing_threat=True,
    ),
    Role.JESTER: _RoleDefinition(
        role=Role.JESTER,
        faction=Faction.JESTER,
        role_family=RoleFamily.DECEPTIVE,
    ),
    Role.VILLAGER: _RoleDefinition(
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        role_family=RoleFamily.VANILLA_TOWN,
        max_count=_ORDINARY_DUPLICATE_MAX,
    ),
}

_REASON_ORDER: Final[tuple[CustomRoleDeckReason, ...]] = (
    CustomRoleDeckReason.ROLE_UNSUPPORTED,
    CustomRoleDeckReason.INVALID_ROLE_COUNT,
    CustomRoleDeckReason.PLAYER_COUNT_MISMATCH,
    CustomRoleDeckReason.PLAYER_COUNT_OUT_OF_RANGE,
    CustomRoleDeckReason.ROLE_GATED,
    CustomRoleDeckReason.ROLE_COUNT_EXCEEDS_MAX,
    CustomRoleDeckReason.ZERO_TOWN,
    CustomRoleDeckReason.ZERO_THREAT,
    CustomRoleDeckReason.INVALID_FACTION_RATIO,
    CustomRoleDeckReason.UNSUPPORTED_SOLO_ROLE_COMBINATION,
    CustomRoleDeckReason.IMPOSSIBLE_WIN_CONDITIONS,
)


def custom_role_deck_capabilities() -> tuple[CustomRoleCapability, ...]:
    """Return the custom-role capability matrix for deck-builder UIs."""
    return tuple(
        CustomRoleCapability(
            role=definition.role,
            faction=definition.faction,
            role_family=definition.role_family,
            available=definition.available,
            reason=None if definition.available else definition.unavailable_reason,
            night_action=definition.night_action,
            max_count=definition.max_count,
            duplicate_allowed=definition.duplicate_allowed,
        )
        for definition in _role_definitions_sorted()
    )


def validate_custom_role_deck(
    role_counts: RoleCountsInput,
    *,
    player_count: int | None = None,
) -> CustomRoleDeckValidation:
    """Validate ``role_counts`` and return a custom rules snapshot on success."""
    normalized, reasons = _normalize_role_counts(role_counts)
    total_count = sum(normalized.values())
    requested_count = total_count if player_count is None else player_count

    if player_count is not None and player_count != total_count:
        reasons.add(CustomRoleDeckReason.PLAYER_COUNT_MISMATCH)
    if requested_count < MIN_CUSTOM_PLAYER_COUNT or requested_count > MAX_CUSTOM_PLAYER_COUNT:
        reasons.add(CustomRoleDeckReason.PLAYER_COUNT_OUT_OF_RANGE)

    _validate_role_capabilities(normalized, reasons)
    _validate_factions_and_win_conditions(normalized, reasons)

    ordered_reasons = _ordered_reasons(reasons)
    if ordered_reasons:
        return CustomRoleDeckValidation(ok=False, reasons=ordered_reasons)

    return CustomRoleDeckValidation(
        ok=True,
        reasons=(),
        snapshot=_build_snapshot(normalized, player_count=requested_count),
    )


def _role_definitions_sorted() -> tuple[_RoleDefinition, ...]:
    return tuple(_role_definition(role) for role in sorted(Role, key=lambda item: item.value))


def _role_definition(role: Role) -> _RoleDefinition:
    definition = _BASE_ROLE_DEFINITIONS[role]
    if role is Role.FRAMER and CURRENT_FRAMER_VARIANCE_GATE.enabled:
        return replace(definition, available=True, unavailable_reason=None)
    if role is Role.MAYOR and CURRENT_MAYOR_VARIANCE_GATE.enabled:
        return replace(definition, available=True, unavailable_reason=None)
    return definition


def _normalize_role_counts(
    role_counts: RoleCountsInput,
) -> tuple[dict[Role, int], set[CustomRoleDeckReason]]:
    normalized: dict[Role, int] = {}
    reasons: set[CustomRoleDeckReason] = set()
    for raw_role, count in role_counts.items():
        role = _parse_role(raw_role)
        if role is None:
            reasons.add(CustomRoleDeckReason.ROLE_UNSUPPORTED)
            continue
        if count <= 0:
            reasons.add(CustomRoleDeckReason.INVALID_ROLE_COUNT)
            continue
        normalized[role] = normalized.get(role, 0) + count
    return normalized, reasons


def _parse_role(raw_role: Role | str) -> Role | None:
    if isinstance(raw_role, Role):
        return raw_role
    try:
        return Role(raw_role)
    except ValueError:
        return None


def _validate_role_capabilities(
    role_counts: Mapping[Role, int],
    reasons: set[CustomRoleDeckReason],
) -> None:
    for role, count in role_counts.items():
        definition = _role_definition(role)
        if not definition.available:
            reasons.add(definition.unavailable_reason or CustomRoleDeckReason.ROLE_GATED)
        if definition.max_count is not None and count > definition.max_count:
            reasons.add(CustomRoleDeckReason.ROLE_COUNT_EXCEEDS_MAX)


def _validate_factions_and_win_conditions(
    role_counts: Mapping[Role, int],
    reasons: set[CustomRoleDeckReason],
) -> None:
    counts = _faction_counts(role_counts)
    town_count = counts.get(Faction.TOWN, 0)
    mafia_count = counts.get(Faction.MAFIA, 0)
    serial_killer_count = counts.get(Faction.SERIAL_KILLER, 0)
    jester_count = counts.get(Faction.JESTER, 0)
    threat_count = mafia_count + serial_killer_count

    if town_count == 0:
        reasons.add(CustomRoleDeckReason.ZERO_TOWN)
    if threat_count == 0:
        reasons.add(CustomRoleDeckReason.ZERO_THREAT)
    if threat_count > 0 and town_count > 0 and threat_count >= town_count:
        reasons.add(CustomRoleDeckReason.INVALID_FACTION_RATIO)
    if serial_killer_count + jester_count > 1:
        reasons.add(CustomRoleDeckReason.UNSUPPORTED_SOLO_ROLE_COMBINATION)
    if threat_count > 0 and not _has_killing_threat(role_counts):
        reasons.add(CustomRoleDeckReason.IMPOSSIBLE_WIN_CONDITIONS)


def _faction_counts(role_counts: Mapping[Role, int]) -> dict[Faction, int]:
    counts: dict[Faction, int] = {}
    for role, count in role_counts.items():
        definition = _role_definition(role)
        counts[definition.faction] = counts.get(definition.faction, 0) + count
    return counts


def _has_killing_threat(role_counts: Mapping[Role, int]) -> bool:
    return any(_role_definition(role).killing_threat for role in role_counts)


def _ordered_reasons(
    reasons: set[CustomRoleDeckReason],
) -> tuple[CustomRoleDeckReason, ...]:
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _build_snapshot(role_counts: Mapping[Role, int], *, player_count: int) -> GameRulesSnapshot:
    roles = tuple(sorted(role_counts, key=lambda role: role.value))
    win_conditions, alt_win_conditions, solo_factions, context_kind, display_label = (
        _snapshot_win_template(role_counts)
    )
    return GameRulesSnapshot(
        ruleset_id=None,
        rating_context_kind=context_kind,
        is_canonical=False,
        rating_context_display_label=display_label,
        player_count=player_count,
        role_counts=tuple(RoleCountSnapshot(role=role, count=role_counts[role]) for role in roles),
        role_factions=tuple(
            RoleFactionSnapshot(role=role, faction=_role_definition(role).faction) for role in roles
        ),
        role_families=tuple(
            RoleFamilySnapshot(role=role, role_family=_role_definition(role).role_family)
            for role in roles
        ),
        role_night_actions=tuple(
            RoleNightActionSnapshot(role=role, action_type=definition.night_action)
            for role in roles
            if (definition := _role_definition(role)).night_action is not None
        ),
        win_conditions=tuple(
            WinConditionSnapshot.from_win_condition(condition) for condition in win_conditions
        ),
        alt_win_conditions=alt_win_conditions,
        solo_factions=solo_factions,
        faction_mutation_allowed=False,
        kingmaking_objective=False,
        discussion_rounds_per_day=CUSTOM_DISCUSSION_ROUNDS_PER_DAY,
        max_days=CUSTOM_MAX_DAYS,
        message_limits=MessageLimitSnapshot(
            public_message_max_chars=CUSTOM_PUBLIC_MESSAGE_MAX_CHARS,
            private_message_max_chars=CUSTOM_PRIVATE_MESSAGE_MAX_CHARS,
            memory_update_max_chars=CUSTOM_MEMORY_UPDATE_MAX_CHARS,
            public_transcript_recent_message_limit=CUSTOM_PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT,
        ),
        cadence_id=BUILTIN_CADENCE_ID,
        agent_response_policy=AgentResponsePolicySnapshot(
            llm_timeout_seconds=CUSTOM_LLM_TIMEOUT_SECONDS,
            temperature=CUSTOM_TEMPERATURE,
            top_p=CUSTOM_TOP_P,
        ),
    )


def _snapshot_win_template(
    role_counts: Mapping[Role, int],
) -> tuple[
    tuple[WinCondition, ...],
    tuple[str, ...],
    tuple[Faction, ...],
    RatingContextKind,
    str,
]:
    has_mafia = any(_role_definition(role).faction is Faction.MAFIA for role in role_counts)
    has_serial_killer = Role.SERIAL_KILLER in role_counts
    has_jester = Role.JESTER in role_counts

    if has_serial_killer:
        return (
            _serial_killer_win_conditions(has_mafia=has_mafia),
            (),
            (Faction.SERIAL_KILLER,),
            RatingContextKind.PLACEMENT,
            CUSTOM_PLACEMENT_DISPLAY_LABEL,
        )
    if has_jester:
        return (
            _jester_win_conditions(),
            (JESTER_DAY_VOTED_OUT_TRIGGER,),
            (Faction.JESTER,),
            RatingContextKind.SOLO_RATE,
            CUSTOM_SOLO_RATE_DISPLAY_LABEL,
        )
    return (
        canonical_two_faction_win_conditions(),
        (),
        (),
        RatingContextKind.CANONICAL_TEAM,
        CUSTOM_TEAM_DISPLAY_LABEL,
    )


def _jester_win_conditions() -> tuple[WinCondition, ...]:
    return (
        WinCondition(
            kind=WinConditionKind.ALT_TRIGGER,
            winner=Faction.JESTER.value,
            reason=REASON_JESTER_DAY_VOTED_OUT,
            trigger=JESTER_DAY_VOTED_OUT_TRIGGER,
        ),
        *canonical_two_faction_win_conditions(),
    )


def _serial_killer_win_conditions(*, has_mafia: bool) -> tuple[WinCondition, ...]:
    target_factions = (
        (Faction.MAFIA, Faction.SERIAL_KILLER) if has_mafia else (Faction.SERIAL_KILLER,)
    )
    conditions: list[WinCondition] = [
        WinCondition(
            kind=WinConditionKind.TARGET_FACTIONS_ELIMINATED,
            winner=Faction.TOWN.value,
            reason=REASON_ALL_THREATS_ELIMINATED,
            target_factions=target_factions,
        ),
        WinCondition(
            kind=WinConditionKind.SOLO_LAST_ALIVE,
            winner=Faction.SERIAL_KILLER.value,
            reason=REASON_SOLO_LAST_ALIVE,
            faction=Faction.SERIAL_KILLER,
        ),
    ]
    if has_mafia:
        conditions.append(
            WinCondition(
                kind=WinConditionKind.FACTION_PARITY,
                winner=Faction.MAFIA.value,
                reason=REASON_PARITY_REACHED,
                faction=Faction.MAFIA,
                opponent_factions=(Faction.TOWN,),
                blocked_by_alive_factions=(Faction.SERIAL_KILLER,),
            )
        )
    conditions.append(
        WinCondition(
            kind=WinConditionKind.DAY_CAP,
            winner="DRAW",
            reason=REASON_MAX_DAYS_REACHED,
        )
    )
    return tuple(conditions)


__all__ = [
    "CUSTOM_DISCUSSION_ROUNDS_PER_DAY",
    "CUSTOM_LLM_TIMEOUT_SECONDS",
    "CUSTOM_MAX_DAYS",
    "CUSTOM_MEMORY_UPDATE_MAX_CHARS",
    "CUSTOM_PLACEMENT_DISPLAY_LABEL",
    "CUSTOM_PRIVATE_MESSAGE_MAX_CHARS",
    "CUSTOM_PUBLIC_MESSAGE_MAX_CHARS",
    "CUSTOM_PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT",
    "CUSTOM_SOLO_RATE_DISPLAY_LABEL",
    "CUSTOM_TEAM_DISPLAY_LABEL",
    "CUSTOM_TEMPERATURE",
    "CUSTOM_TOP_P",
    "MAX_CUSTOM_PLAYER_COUNT",
    "MIN_CUSTOM_PLAYER_COUNT",
    "CustomRoleCapability",
    "CustomRoleDeckReason",
    "CustomRoleDeckValidation",
    "custom_role_deck_capabilities",
    "validate_custom_role_deck",
]
