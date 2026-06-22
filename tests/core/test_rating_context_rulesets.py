"""US-171: rulesets declare their rating context explicitly."""

from __future__ import annotations

import pytest

from padrino.core.enums import Faction, RatingContextKind, Role, RoleFamily
from padrino.core.rulesets import (
    Ruleset,
    bench10_v1,
    deception13_v1,
    get_ruleset,
    mini7_v1,
    roleblock10_v1,
    sk12_v1,
)
from padrino.core.rulesets.canonicality import (
    CanonicalRulesetError,
    assert_ruleset_canonical_pure,
    canonical_team_ranks_for_outcome,
)


def test_builtin_rulesets_declare_canonical_team_context() -> None:
    for ruleset_id in (
        mini7_v1.RULESET_ID,
        bench10_v1.RULESET_ID,
        roleblock10_v1.RULESET_ID,
        deception13_v1.RULESET_ID,
    ):
        ruleset = get_ruleset(ruleset_id)

        assert ruleset.RATING_CONTEXT_KIND is RatingContextKind.CANONICAL_TEAM
        assert ruleset.IS_CANONICAL is True
        assert ruleset.RATING_CONTEXT_DISPLAY_LABEL


def test_builtin_canonical_rulesets_are_canonical_pure() -> None:
    for ruleset_id in (
        mini7_v1.RULESET_ID,
        bench10_v1.RULESET_ID,
        roleblock10_v1.RULESET_ID,
        deception13_v1.RULESET_ID,
    ):
        assert_ruleset_canonical_pure(get_ruleset(ruleset_id))


def test_sk12_ruleset_declares_noncanonical_placement_context() -> None:
    ruleset = get_ruleset(sk12_v1.RULESET_ID)

    assert ruleset.RATING_CONTEXT_KIND is RatingContextKind.PLACEMENT
    assert ruleset.IS_CANONICAL is False
    assert ruleset.RATING_CONTEXT_DISPLAY_LABEL
    assert ruleset.SOLO_FACTIONS == ("SERIAL_KILLER",)
    assert ruleset.ROLE_COUNTS[Role.SERIAL_KILLER] == 1
    assert ruleset.ROLE_FACTIONS[Role.SERIAL_KILLER] is Faction.SERIAL_KILLER


def test_sk12_ruleset_fails_canonical_introspection() -> None:
    with pytest.raises(CanonicalRulesetError, match="does not declare CANONICAL_TEAM"):
        assert_ruleset_canonical_pure(sk12_v1)


def test_draw_is_a_tie_between_canonical_teams() -> None:
    ranks = canonical_team_ranks_for_outcome("DRAW")

    assert ranks["TOWN"] == ranks["MAFIA"]


def test_canonical_validator_rejects_alt_win_conditions() -> None:
    class AltWinRuleset:
        RULESET_ID = "bad_alt_win_v1"
        RATING_CONTEXT_KIND = RatingContextKind.CANONICAL_TEAM
        IS_CANONICAL = True
        RATING_CONTEXT_DISPLAY_LABEL = "Bad canonical"
        PLAYER_COUNT = mini7_v1.PLAYER_COUNT
        ROLE_COUNTS = mini7_v1.ROLE_COUNTS
        ROLE_FACTIONS = mini7_v1.ROLE_FACTIONS
        DISCUSSION_ROUNDS_PER_DAY = mini7_v1.DISCUSSION_ROUNDS_PER_DAY
        MAX_DAYS = mini7_v1.MAX_DAYS
        PUBLIC_MESSAGE_MAX_CHARS = mini7_v1.PUBLIC_MESSAGE_MAX_CHARS
        PRIVATE_MESSAGE_MAX_CHARS = mini7_v1.PRIVATE_MESSAGE_MAX_CHARS
        MEMORY_UPDATE_MAX_CHARS = mini7_v1.MEMORY_UPDATE_MAX_CHARS
        PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT = mini7_v1.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
        LLM_TIMEOUT_SECONDS = mini7_v1.LLM_TIMEOUT_SECONDS
        TEMPERATURE = mini7_v1.TEMPERATURE
        TOP_P = mini7_v1.TOP_P
        ALT_WIN_CONDITIONS: tuple[str, ...] = ("JESTER_LYNCHED",)
        SOLO_FACTIONS: tuple[str, ...] = ()
        FACTION_MUTATION_ALLOWED: bool = False
        KINGMAKING_OBJECTIVE: bool = False

        @staticmethod
        def role_family_for(role: Role) -> RoleFamily:
            return mini7_v1.role_family_for(role)

        @staticmethod
        def faction_for(role: Role) -> Faction:
            return mini7_v1.faction_for(role)

    with pytest.raises(CanonicalRulesetError, match="alt-win"):
        assert_ruleset_canonical_pure(AltWinRuleset())


def test_canonical_validator_rejects_solo_or_mutating_rulesets() -> None:
    class SoloMutatingRuleset:
        RULESET_ID = "bad_solo_v1"
        RATING_CONTEXT_KIND = RatingContextKind.CANONICAL_TEAM
        IS_CANONICAL = True
        RATING_CONTEXT_DISPLAY_LABEL = "Bad canonical"
        PLAYER_COUNT = mini7_v1.PLAYER_COUNT
        ROLE_COUNTS = mini7_v1.ROLE_COUNTS
        ROLE_FACTIONS = mini7_v1.ROLE_FACTIONS
        DISCUSSION_ROUNDS_PER_DAY = mini7_v1.DISCUSSION_ROUNDS_PER_DAY
        MAX_DAYS = mini7_v1.MAX_DAYS
        PUBLIC_MESSAGE_MAX_CHARS = mini7_v1.PUBLIC_MESSAGE_MAX_CHARS
        PRIVATE_MESSAGE_MAX_CHARS = mini7_v1.PRIVATE_MESSAGE_MAX_CHARS
        MEMORY_UPDATE_MAX_CHARS = mini7_v1.MEMORY_UPDATE_MAX_CHARS
        PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT = mini7_v1.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
        LLM_TIMEOUT_SECONDS = mini7_v1.LLM_TIMEOUT_SECONDS
        TEMPERATURE = mini7_v1.TEMPERATURE
        TOP_P = mini7_v1.TOP_P
        ALT_WIN_CONDITIONS: tuple[str, ...] = ()
        SOLO_FACTIONS: tuple[str, ...] = ("SERIAL_KILLER",)
        FACTION_MUTATION_ALLOWED: bool = True
        KINGMAKING_OBJECTIVE: bool = False

        @staticmethod
        def role_family_for(role: Role) -> RoleFamily:
            return mini7_v1.role_family_for(role)

        @staticmethod
        def faction_for(role: Role) -> Faction:
            return mini7_v1.faction_for(role)

    with pytest.raises(CanonicalRulesetError, match="solo"):
        assert_ruleset_canonical_pure(SoloMutatingRuleset())


def test_validator_typechecks_against_ruleset_protocol() -> None:
    ruleset: Ruleset = mini7_v1

    assert_ruleset_canonical_pure(ruleset)
