"""Tests for pure custom role-deck validation."""

from __future__ import annotations

from collections import OrderedDict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from padrino.core.enums import Faction, RatingContextKind, Role
from padrino.core.rulesets.custom_deck import (
    MAX_CUSTOM_PLAYER_COUNT,
    MIN_CUSTOM_PLAYER_COUNT,
    CustomRoleCapability,
    CustomRoleDeckReason,
    custom_role_deck_capabilities,
    validate_custom_role_deck,
)
from padrino.core.rulesets.snapshot import canonical_rules_hash, canonical_rules_json


def _capability_by_role() -> dict[Role, CustomRoleCapability]:
    return {capability.role: capability for capability in custom_role_deck_capabilities()}


@given(player_count=st.integers(MIN_CUSTOM_PLAYER_COUNT, MAX_CUSTOM_PLAYER_COUNT))
def test_validator_accepts_team_decks_for_7_to_15_seats(player_count: int) -> None:
    mafia_count = max(1, min(3, (player_count - 3) // 3))
    role_counts = {
        Role.MAFIA_GOON: mafia_count,
        Role.DETECTIVE: 1,
        Role.DOCTOR: 1,
        Role.VILLAGER: player_count - mafia_count - 2,
    }
    reversed_counts = OrderedDict(reversed(tuple(role_counts.items())))

    result = validate_custom_role_deck(role_counts, player_count=player_count)
    reversed_result = validate_custom_role_deck(reversed_counts, player_count=player_count)

    assert result.ok is True
    assert result.reasons == ()
    assert result.snapshot is not None
    assert reversed_result.snapshot is not None
    assert result.snapshot.player_count == player_count
    assert result.snapshot.ruleset_id is None
    assert result.snapshot.RULESET_ID.startswith("custom:")
    assert result.snapshot.rating_context_kind is RatingContextKind.CANONICAL_TEAM
    assert result.snapshot.is_canonical is False
    assert [entry.role for entry in result.snapshot.role_counts] == sorted(
        role_counts,
        key=lambda role: role.value,
    )
    assert canonical_rules_json(result.snapshot) == canonical_rules_json(reversed_result.snapshot)
    assert canonical_rules_hash(result.snapshot) == canonical_rules_hash(reversed_result.snapshot)


@pytest.mark.parametrize(
    ("role_counts", "player_count", "reason"),
    [
        ({Role.MAFIA_GOON: 2, Role.VILLAGER: 5}, 8, CustomRoleDeckReason.PLAYER_COUNT_MISMATCH),
        (
            {Role.MAFIA_GOON: 2, Role.VILLAGER: 4},
            None,
            CustomRoleDeckReason.PLAYER_COUNT_OUT_OF_RANGE,
        ),
        (
            {Role.MAFIA_GOON: 2, Role.VILLAGER: 14},
            None,
            CustomRoleDeckReason.PLAYER_COUNT_OUT_OF_RANGE,
        ),
        (
            {Role.MAFIA_GOON: 2, Role.VILLAGER: 5, Role.DOCTOR: 0},
            None,
            CustomRoleDeckReason.INVALID_ROLE_COUNT,
        ),
        (
            {Role.DOCTOR: 1, Role.DETECTIVE: 1, Role.VILLAGER: 5},
            None,
            CustomRoleDeckReason.ZERO_THREAT,
        ),
        ({Role.MAFIA_GOON: 7}, None, CustomRoleDeckReason.ZERO_TOWN),
        ({Role.MAFIA_GOON: 4, Role.VILLAGER: 3}, None, CustomRoleDeckReason.INVALID_FACTION_RATIO),
        (
            {Role.MAFIA_GOON: 2, Role.SERIAL_KILLER: 1, Role.JESTER: 1, Role.VILLAGER: 7},
            None,
            CustomRoleDeckReason.UNSUPPORTED_SOLO_ROLE_COMBINATION,
        ),
        (
            {Role.MAFIA_ROLEBLOCKER: 2, Role.VILLAGER: 5},
            None,
            CustomRoleDeckReason.IMPOSSIBLE_WIN_CONDITIONS,
        ),
        (
            {"GHOST": 1, Role.MAFIA_GOON: 2, Role.VILLAGER: 5},
            None,
            CustomRoleDeckReason.ROLE_UNSUPPORTED,
        ),
    ],
)
def test_validator_rejects_invalid_decks_with_stable_reason_tokens(
    role_counts: dict[Role | str, int],
    player_count: int | None,
    reason: CustomRoleDeckReason,
) -> None:
    result = validate_custom_role_deck(role_counts, player_count=player_count)

    assert result.ok is False
    assert result.snapshot is None
    assert reason in result.reasons
    assert all(
        isinstance(token.value, str) and token.value == token.value.lower()
        for token in result.reasons
    )


def test_validator_allows_duplicate_ordinary_roles_and_pins_unique_special_roles() -> None:
    duplicate_ordinary = validate_custom_role_deck(
        {
            Role.MAFIA_GOON: 3,
            Role.DETECTIVE: 1,
            Role.DOCTOR: 1,
            Role.VILLAGER: 7,
        }
    )
    duplicate_detectives = validate_custom_role_deck(
        {
            Role.MAFIA_GOON: 2,
            Role.DETECTIVE: 2,
            Role.DOCTOR: 1,
            Role.VILLAGER: 5,
        }
    )

    assert duplicate_ordinary.ok is True
    assert duplicate_detectives.ok is False
    assert duplicate_detectives.reasons == (CustomRoleDeckReason.ROLE_COUNT_EXCEEDS_MAX,)


@pytest.mark.parametrize("role", [Role.MAYOR, Role.FRAMER, Role.COMMUTER, Role.MASON])
def test_gated_roles_are_unavailable_from_same_capability_surface(role: Role) -> None:
    capabilities = _capability_by_role()
    capability = capabilities[role]
    role_counts = {
        Role.MAFIA_GOON: 2,
        role: 1,
        Role.DETECTIVE: 1,
        Role.DOCTOR: 1,
        Role.VILLAGER: 4,
    }

    result = validate_custom_role_deck(role_counts)

    assert capability.available is False
    assert capability.reason is CustomRoleDeckReason.ROLE_GATED
    assert result.ok is False
    assert result.reasons == (CustomRoleDeckReason.ROLE_GATED,)


@pytest.mark.parametrize(
    ("role", "role_counts"),
    [
        (
            Role.MAFIA_GOON,
            {Role.MAFIA_GOON: 2, Role.DETECTIVE: 1, Role.DOCTOR: 1, Role.VILLAGER: 3},
        ),
        (
            Role.NINJA,
            {
                Role.NINJA: 1,
                Role.MAFIA_GOON: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
        ),
        (
            Role.GODFATHER,
            {
                Role.GODFATHER: 1,
                Role.MAFIA_GOON: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
        ),
        (
            Role.MAFIA_ROLEBLOCKER,
            {
                Role.MAFIA_ROLEBLOCKER: 1,
                Role.MAFIA_GOON: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
        ),
        (
            Role.JANITOR,
            {
                Role.JANITOR: 1,
                Role.MAFIA_GOON: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
        ),
        (Role.DETECTIVE, {Role.MAFIA_GOON: 2, Role.DETECTIVE: 1, Role.DOCTOR: 1, Role.VILLAGER: 3}),
        (Role.DOCTOR, {Role.MAFIA_GOON: 2, Role.DETECTIVE: 1, Role.DOCTOR: 1, Role.VILLAGER: 3}),
        (
            Role.TRACKER,
            {
                Role.MAFIA_GOON: 2,
                Role.TRACKER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 2,
            },
        ),
        (
            Role.WATCHER,
            {
                Role.MAFIA_GOON: 2,
                Role.WATCHER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 2,
            },
        ),
        (
            Role.SERIAL_KILLER,
            {
                Role.MAFIA_GOON: 1,
                Role.SERIAL_KILLER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 4,
            },
        ),
        (
            Role.JESTER,
            {
                Role.MAFIA_GOON: 2,
                Role.JESTER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
        ),
        (Role.VILLAGER, {Role.MAFIA_GOON: 2, Role.DETECTIVE: 1, Role.DOCTOR: 1, Role.VILLAGER: 3}),
    ],
)
def test_every_available_role_can_enter_a_supported_deck(
    role: Role,
    role_counts: dict[Role, int],
) -> None:
    capabilities = _capability_by_role()

    result = validate_custom_role_deck(role_counts)

    assert capabilities[role].available is True
    assert result.ok is True
    assert result.snapshot is not None
    assert role in result.snapshot.ROLE_COUNTS


@pytest.mark.parametrize(
    ("role_counts", "expected_kind", "expected_solo"),
    [
        (
            {
                Role.MAFIA_GOON: 1,
                Role.SERIAL_KILLER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 4,
            },
            RatingContextKind.PLACEMENT,
            (Faction.SERIAL_KILLER,),
        ),
        (
            {
                Role.MAFIA_GOON: 2,
                Role.JESTER: 1,
                Role.DETECTIVE: 1,
                Role.DOCTOR: 1,
                Role.VILLAGER: 3,
            },
            RatingContextKind.SOLO_RATE,
            (Faction.JESTER,),
        ),
    ],
)
def test_supported_solo_role_decks_get_matching_win_template(
    role_counts: dict[Role, int],
    expected_kind: RatingContextKind,
    expected_solo: tuple[Faction, ...],
) -> None:
    result = validate_custom_role_deck(role_counts)

    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.rating_context_kind is expected_kind
    assert result.snapshot.solo_factions == expected_solo
    assert expected_solo[0].value in {
        condition.winner for condition in result.snapshot.win_conditions
    }


@pytest.mark.parametrize(
    "role_counts",
    [
        {
            Role.NINJA: 1,
            Role.MAFIA_GOON: 1,
            Role.TRACKER: 1,
            Role.WATCHER: 1,
            Role.DOCTOR: 1,
            Role.VILLAGER: 4,
        },
        {
            Role.GODFATHER: 1,
            Role.MAFIA_GOON: 1,
            Role.DETECTIVE: 1,
            Role.DOCTOR: 1,
            Role.VILLAGER: 3,
        },
        {
            Role.JANITOR: 1,
            Role.MAFIA_GOON: 1,
            Role.SERIAL_KILLER: 1,
            Role.DOCTOR: 1,
            Role.VILLAGER: 5,
        },
        {
            Role.MAFIA_ROLEBLOCKER: 1,
            Role.MAFIA_GOON: 1,
            Role.TRACKER: 1,
            Role.WATCHER: 1,
            Role.VILLAGER: 4,
        },
    ],
)
def test_pairwise_interaction_sensitive_decks_are_accepted(
    role_counts: dict[Role, int],
) -> None:
    result = validate_custom_role_deck(role_counts)

    assert result.ok is True
    assert result.snapshot is not None
