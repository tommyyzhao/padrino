"""Tests for deception13_v1 ruleset constants and role helpers."""

from __future__ import annotations

import padrino.core.rulesets.deception13_v1 as deception13
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily
from padrino.core.rulesets.canonicality import assert_ruleset_canonical_pure
from padrino.core.rulesets.deception13_v1 import faction_for, role_family_for
from padrino.core.rulesets.framer_variance_gate import CURRENT_FRAMER_VARIANCE_GATE


def test_role_counts_sum_to_player_count() -> None:
    assert sum(deception13.ROLE_COUNTS.values()) == deception13.PLAYER_COUNT


def test_role_counts_exact_and_vanilla_majority() -> None:
    assert deception13.ROLE_COUNTS[Role.GODFATHER] == 1
    assert deception13.ROLE_COUNTS[Role.MAFIA_ROLEBLOCKER] == 1
    assert deception13.ROLE_COUNTS[Role.JANITOR] == 1
    assert deception13.ROLE_COUNTS[Role.MAFIA_GOON] == 1
    assert deception13.ROLE_COUNTS[Role.DETECTIVE] == 1
    assert deception13.ROLE_COUNTS[Role.DOCTOR] == 1
    assert deception13.ROLE_COUNTS[Role.VILLAGER] == 7
    assert Role.FRAMER not in deception13.ROLE_COUNTS
    assert deception13.ROLE_COUNTS[Role.VILLAGER] > sum(
        count
        for role, count in deception13.ROLE_COUNTS.items()
        if deception13.faction_for(role) is Faction.MAFIA
    )


def test_framer_stays_excluded_until_variance_gate_passes() -> None:
    assert CURRENT_FRAMER_VARIANCE_GATE.enabled is False
    assert Role.FRAMER not in deception13.ROLE_COUNTS


def test_power_roles_are_a_minority_of_all_seats() -> None:
    power_roles = {
        Role.GODFATHER,
        Role.MAFIA_ROLEBLOCKER,
        Role.JANITOR,
        Role.DETECTIVE,
        Role.DOCTOR,
    }
    power_count = sum(deception13.ROLE_COUNTS.get(role, 0) for role in power_roles)

    assert power_count < deception13.PLAYER_COUNT / 2


def test_declares_canonical_team_context_and_is_canonical_pure() -> None:
    assert deception13.RATING_CONTEXT_KIND is RatingContextKind.CANONICAL_TEAM
    assert deception13.IS_CANONICAL is True
    assert deception13.RATING_CONTEXT_DISPLAY_LABEL
    assert_ruleset_canonical_pure(deception13)


def test_all_roles_have_a_family() -> None:
    for role in deception13.ROLE_COUNTS:
        family = role_family_for(role)
        assert isinstance(family, RoleFamily)


def test_all_roles_have_a_faction() -> None:
    for role in deception13.ROLE_COUNTS:
        faction = faction_for(role)
        assert isinstance(faction, Faction)


def test_role_family_assignments() -> None:
    assert role_family_for(Role.MAFIA_GOON) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.GODFATHER) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.MAFIA_ROLEBLOCKER) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.JANITOR) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.DETECTIVE) == RoleFamily.INVESTIGATIVE
    assert role_family_for(Role.DOCTOR) == RoleFamily.PROTECTIVE
    assert role_family_for(Role.VILLAGER) == RoleFamily.VANILLA_TOWN


def test_faction_assignments() -> None:
    assert faction_for(Role.MAFIA_GOON) == Faction.MAFIA
    assert faction_for(Role.GODFATHER) == Faction.MAFIA
    assert faction_for(Role.MAFIA_ROLEBLOCKER) == Faction.MAFIA
    assert faction_for(Role.JANITOR) == Faction.MAFIA
    assert faction_for(Role.DETECTIVE) == Faction.TOWN
    assert faction_for(Role.DOCTOR) == Faction.TOWN
    assert faction_for(Role.VILLAGER) == Faction.TOWN


def test_ruleset_importable_as_frozen_namespace() -> None:
    assert deception13.RULESET_ID == "deception13_v1"
    assert deception13.PLAYER_COUNT == 13
    assert deception13.MAX_DAYS == 5
    assert deception13.DISCUSSION_ROUNDS_PER_DAY == 3
    assert deception13.PUBLIC_MESSAGE_MAX_CHARS == 600
    assert deception13.PRIVATE_MESSAGE_MAX_CHARS == 600
    assert deception13.MEMORY_UPDATE_MAX_CHARS == 1200
    assert deception13.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT == 80
    assert deception13.LLM_TIMEOUT_SECONDS == 45
    assert deception13.TEMPERATURE == 0.7
    assert deception13.TOP_P == 1.0
