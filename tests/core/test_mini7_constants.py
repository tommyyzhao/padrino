"""Tests for mini7_v1 ruleset constants and role helpers."""

from __future__ import annotations

import padrino.core.rulesets.mini7_v1 as mini7
from padrino.core.enums import Faction, Role, RoleFamily
from padrino.core.rulesets.mini7_v1 import faction_for, role_family_for


def test_role_counts_sum_to_player_count() -> None:
    assert sum(mini7.ROLE_COUNTS.values()) == mini7.PLAYER_COUNT


def test_role_counts_exact() -> None:
    assert mini7.ROLE_COUNTS[Role.MAFIA_GOON] == 2
    assert mini7.ROLE_COUNTS[Role.DETECTIVE] == 1
    assert mini7.ROLE_COUNTS[Role.DOCTOR] == 1
    assert mini7.ROLE_COUNTS[Role.VILLAGER] == 3


def test_all_roles_have_a_family() -> None:
    for role in Role:
        family = role_family_for(role)
        assert isinstance(family, RoleFamily)


def test_all_roles_have_a_faction() -> None:
    for role in Role:
        faction = faction_for(role)
        assert isinstance(faction, Faction)


def test_role_family_assignments() -> None:
    assert role_family_for(Role.MAFIA_GOON) == RoleFamily.DECEPTIVE
    assert role_family_for(Role.DETECTIVE) == RoleFamily.INVESTIGATIVE
    assert role_family_for(Role.DOCTOR) == RoleFamily.PROTECTIVE
    assert role_family_for(Role.VILLAGER) == RoleFamily.VANILLA_TOWN


def test_faction_assignments() -> None:
    assert faction_for(Role.MAFIA_GOON) == Faction.MAFIA
    assert faction_for(Role.DETECTIVE) == Faction.TOWN
    assert faction_for(Role.DOCTOR) == Faction.TOWN
    assert faction_for(Role.VILLAGER) == Faction.TOWN


def test_ruleset_importable_as_frozen_namespace() -> None:
    assert mini7.RULESET_ID == "mini7_v1"
    assert mini7.PLAYER_COUNT == 7
    assert mini7.MAX_DAYS == 5
    assert mini7.DISCUSSION_ROUNDS_PER_DAY == 3
    assert mini7.PUBLIC_MESSAGE_MAX_CHARS == 600
    assert mini7.PRIVATE_MESSAGE_MAX_CHARS == 600
    assert mini7.MEMORY_UPDATE_MAX_CHARS == 1200
    assert mini7.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT == 80
    assert mini7.LLM_TIMEOUT_SECONDS == 45
    assert mini7.TEMPERATURE == 0.7
    assert mini7.TOP_P == 1.0
