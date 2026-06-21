"""US-171: rulesets declare their rating context explicitly."""

from __future__ import annotations

from padrino.core.enums import RatingContextKind
from padrino.core.rulesets import bench10_v1, get_ruleset, mini7_v1


def test_builtin_rulesets_declare_canonical_team_context() -> None:
    for ruleset_id in (mini7_v1.RULESET_ID, bench10_v1.RULESET_ID):
        ruleset = get_ruleset(ruleset_id)

        assert ruleset.RATING_CONTEXT_KIND is RatingContextKind.CANONICAL_TEAM
        assert ruleset.IS_CANONICAL is True
        assert ruleset.RATING_CONTEXT_DISPLAY_LABEL
