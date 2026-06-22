"""Tests for ninja13_v1 ruleset constants and role helpers."""

from __future__ import annotations

import padrino.core.rulesets.ninja13_v1 as ninja13
from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily
from padrino.core.rulesets.canonicality import assert_ruleset_canonical_pure
from padrino.core.rulesets.ninja13_v1 import faction_for, role_family_for


def test_role_counts_sum_to_player_count() -> None:
    assert sum(ninja13.ROLE_COUNTS.values()) == ninja13.PLAYER_COUNT


def test_role_counts_exact_and_two_faction_clean() -> None:
    assert ninja13.ROLE_COUNTS[Role.NINJA] == 1
    assert ninja13.ROLE_COUNTS[Role.MAFIA_GOON] == 1
    assert ninja13.ROLE_COUNTS[Role.MAFIA_ROLEBLOCKER] == 1
    assert ninja13.ROLE_COUNTS[Role.DETECTIVE] == 1
    assert ninja13.ROLE_COUNTS[Role.DOCTOR] == 1
    assert ninja13.ROLE_COUNTS[Role.TRACKER] == 1
    assert ninja13.ROLE_COUNTS[Role.WATCHER] == 1
    assert ninja13.ROLE_COUNTS[Role.VILLAGER] == 6
    assert Role.FRAMER not in ninja13.ROLE_COUNTS
    assert Role.SERIAL_KILLER not in ninja13.ROLE_COUNTS
    assert Role.JESTER not in ninja13.ROLE_COUNTS
    assert set(ninja13.ROLE_FACTIONS.values()) == {Faction.TOWN, Faction.MAFIA}


def test_power_roles_are_a_minority_of_all_seats() -> None:
    power_roles = {
        Role.NINJA,
        Role.MAFIA_ROLEBLOCKER,
        Role.DETECTIVE,
        Role.DOCTOR,
        Role.TRACKER,
        Role.WATCHER,
    }
    power_count = sum(ninja13.ROLE_COUNTS.get(role, 0) for role in power_roles)

    assert power_count < ninja13.PLAYER_COUNT / 2


def test_declares_canonical_team_context_and_is_canonical_pure() -> None:
    assert ninja13.RATING_CONTEXT_KIND is RatingContextKind.CANONICAL_TEAM
    assert ninja13.IS_CANONICAL is True
    assert ninja13.RATING_CONTEXT_DISPLAY_LABEL
    assert_ruleset_canonical_pure(ninja13)


def test_all_roles_have_a_family() -> None:
    for role in ninja13.ROLE_COUNTS:
        family = role_family_for(role)
        assert isinstance(family, RoleFamily)


def test_all_roles_have_a_faction() -> None:
    for role in ninja13.ROLE_COUNTS:
        faction = faction_for(role)
        assert isinstance(faction, Faction)


def test_role_family_assignments() -> None:
    assert role_family_for(Role.NINJA) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.MAFIA_GOON) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.MAFIA_ROLEBLOCKER) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.DETECTIVE) == RoleFamily.INVESTIGATIVE
    assert role_family_for(Role.DOCTOR) == RoleFamily.PROTECTIVE
    assert role_family_for(Role.TRACKER) == RoleFamily.INVESTIGATIVE
    assert role_family_for(Role.WATCHER) == RoleFamily.INVESTIGATIVE
    assert role_family_for(Role.VILLAGER) == RoleFamily.VANILLA_TOWN


def test_faction_assignments() -> None:
    assert faction_for(Role.NINJA) == Faction.MAFIA
    assert faction_for(Role.MAFIA_GOON) == Faction.MAFIA
    assert faction_for(Role.MAFIA_ROLEBLOCKER) == Faction.MAFIA
    assert faction_for(Role.DETECTIVE) == Faction.TOWN
    assert faction_for(Role.DOCTOR) == Faction.TOWN
    assert faction_for(Role.TRACKER) == Faction.TOWN
    assert faction_for(Role.WATCHER) == Faction.TOWN
    assert faction_for(Role.VILLAGER) == Faction.TOWN


def test_ruleset_importable_as_frozen_namespace() -> None:
    assert ninja13.RULESET_ID == "ninja13_v1"
    assert ninja13.PLAYER_COUNT == 13
    assert ninja13.MAX_DAYS == 5
    assert ninja13.DISCUSSION_ROUNDS_PER_DAY == 3
    assert ninja13.PUBLIC_MESSAGE_MAX_CHARS == 600
    assert ninja13.PRIVATE_MESSAGE_MAX_CHARS == 600
    assert ninja13.MEMORY_UPDATE_MAX_CHARS == 1200
    assert ninja13.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT == 80
    assert ninja13.LLM_TIMEOUT_SECONDS == 45
    assert ninja13.TEMPERATURE == 0.7
    assert ninja13.TOP_P == 1.0
