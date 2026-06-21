"""Tests for bench10_v1 ruleset constants and role helpers."""

from __future__ import annotations

import padrino.core.rulesets.bench10_v1 as bench10
from padrino.core.enums import Faction, Role, RoleFamily
from padrino.core.rulesets.bench10_v1 import faction_for, role_family_for


def test_role_counts_sum_to_player_count() -> None:
    assert sum(bench10.ROLE_COUNTS.values()) == bench10.PLAYER_COUNT


def test_role_counts_exact() -> None:
    assert bench10.ROLE_COUNTS[Role.MAFIA_GOON] == 3
    assert bench10.ROLE_COUNTS[Role.DETECTIVE] == 1
    assert bench10.ROLE_COUNTS[Role.DOCTOR] == 1
    assert bench10.ROLE_COUNTS[Role.VILLAGER] == 5


def test_all_roles_have_a_family() -> None:
    for role in bench10.ROLE_COUNTS:
        family = role_family_for(role)
        assert isinstance(family, RoleFamily)


def test_all_roles_have_a_faction() -> None:
    for role in bench10.ROLE_COUNTS:
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
    assert bench10.RULESET_ID == "bench10_v1"
    assert bench10.PLAYER_COUNT == 10
    assert bench10.MAX_DAYS == 5
    assert bench10.DISCUSSION_ROUNDS_PER_DAY == 3
    assert bench10.PUBLIC_MESSAGE_MAX_CHARS == 600
    assert bench10.PRIVATE_MESSAGE_MAX_CHARS == 600
    assert bench10.MEMORY_UPDATE_MAX_CHARS == 1200
    assert bench10.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT == 80
    assert bench10.LLM_TIMEOUT_SECONDS == 45
    assert bench10.TEMPERATURE == 0.7
    assert bench10.TOP_P == 1.0
