"""Ranked human eligibility predicate tests (US-290)."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from padrino.core.enums import LeagueKind
from padrino.core.ranked_eligibility import (
    RankedConsentFacts,
    RankedEligibilityInputs,
    RankedEligibilityReason,
    evaluate_ranked_eligibility,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "padrino" / "core" / "ranked_eligibility.py"
)


def _passing_facts() -> RankedEligibilityInputs:
    return RankedEligibilityInputs(
        ranked_human_rating_enabled=True,
        principal_kind="account",
        email_verified=True,
        has_current_base_consent=True,
        ranked_consent=RankedConsentFacts(has_current_ranked_consent=True),
        ranked_banned=False,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
    )


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (
            replace(_passing_facts(), ranked_human_rating_enabled=False),
            RankedEligibilityReason.RANKED_DISABLED,
        ),
        (
            replace(_passing_facts(), principal_kind="guest"),
            RankedEligibilityReason.GUEST_PRINCIPAL,
        ),
        (replace(_passing_facts(), email_verified=False), RankedEligibilityReason.EMAIL_UNVERIFIED),
        (
            replace(_passing_facts(), has_current_base_consent=False),
            RankedEligibilityReason.BASE_CONSENT_MISSING,
        ),
        (
            replace(
                _passing_facts(),
                ranked_consent=RankedConsentFacts(has_current_ranked_consent=False),
            ),
            RankedEligibilityReason.RANKED_CONSENT_MISSING,
        ),
        (replace(_passing_facts(), ranked_banned=True), RankedEligibilityReason.RANKED_BANNED),
        (
            replace(_passing_facts(), league_kind=LeagueKind.SCIENTIFIC),
            RankedEligibilityReason.LEAGUE_NOT_HUMANS_INCLUDED_RANKED,
        ),
        (
            replace(_passing_facts(), league_ranked=False),
            RankedEligibilityReason.LEAGUE_NOT_HUMANS_INCLUDED_RANKED,
        ),
    ],
)
def test_each_single_failure_returns_machine_reason(
    facts: RankedEligibilityInputs,
    reason: RankedEligibilityReason,
) -> None:
    decision = evaluate_ranked_eligibility(facts)

    assert decision.allowed is False
    assert decision.reason is reason
    assert isinstance(decision.reason, str)


def test_all_preconditions_allow_ranked_play() -> None:
    decision = evaluate_ranked_eligibility(_passing_facts())

    assert decision.allowed is True
    assert decision.reason is RankedEligibilityReason.ELIGIBLE


def test_reason_precedence_is_deterministic() -> None:
    facts = RankedEligibilityInputs(
        ranked_human_rating_enabled=False,
        principal_kind="guest",
        email_verified=False,
        has_current_base_consent=False,
        ranked_consent=RankedConsentFacts(has_current_ranked_consent=False),
        ranked_banned=True,
        league_kind=LeagueKind.SCIENTIFIC,
        league_ranked=False,
    )

    assert evaluate_ranked_eligibility(facts).reason is RankedEligibilityReason.RANKED_DISABLED
    assert (
        evaluate_ranked_eligibility(replace(facts, ranked_human_rating_enabled=True)).reason
        is RankedEligibilityReason.GUEST_PRINCIPAL
    )
    assert (
        evaluate_ranked_eligibility(
            replace(facts, ranked_human_rating_enabled=True, principal_kind="account")
        ).reason
        is RankedEligibilityReason.EMAIL_UNVERIFIED
    )
    assert (
        evaluate_ranked_eligibility(
            replace(
                facts,
                ranked_human_rating_enabled=True,
                principal_kind="account",
                email_verified=True,
            )
        ).reason
        is RankedEligibilityReason.BASE_CONSENT_MISSING
    )
    assert (
        evaluate_ranked_eligibility(
            replace(
                facts,
                ranked_human_rating_enabled=True,
                principal_kind="account",
                email_verified=True,
                has_current_base_consent=True,
            )
        ).reason
        is RankedEligibilityReason.RANKED_CONSENT_MISSING
    )
    assert (
        evaluate_ranked_eligibility(
            replace(
                facts,
                ranked_human_rating_enabled=True,
                principal_kind="account",
                email_verified=True,
                has_current_base_consent=True,
                ranked_consent=RankedConsentFacts(has_current_ranked_consent=True),
            )
        ).reason
        is RankedEligibilityReason.RANKED_BANNED
    )
    assert (
        evaluate_ranked_eligibility(
            replace(
                facts,
                ranked_human_rating_enabled=True,
                principal_kind="account",
                email_verified=True,
                has_current_base_consent=True,
                ranked_consent=RankedConsentFacts(has_current_ranked_consent=True),
                ranked_banned=False,
            )
        ).reason
        is RankedEligibilityReason.LEAGUE_NOT_HUMANS_INCLUDED_RANKED
    )


@pytest.mark.parametrize(
    "facts",
    [
        None,
        RankedEligibilityInputs(),
        replace(_passing_facts(), principal_kind=None),
        replace(_passing_facts(), email_verified=None),
        replace(_passing_facts(), has_current_base_consent=None),
        replace(_passing_facts(), ranked_consent=None),
        replace(_passing_facts(), ranked_banned=None),
        replace(_passing_facts(), league_kind=None),
        replace(_passing_facts(), league_ranked=None),
    ],
)
def test_none_or_missing_inputs_fail_closed_without_raising(
    facts: RankedEligibilityInputs | None,
) -> None:
    decision = evaluate_ranked_eligibility(facts)

    assert decision.allowed is False
    assert isinstance(decision.reason, RankedEligibilityReason)


def test_ranked_consent_facts_are_typed_forward_compatible_slot() -> None:
    facts = _passing_facts()

    assert isinstance(facts.ranked_consent, RankedConsentFacts)
    assert facts.ranked_consent.has_current_ranked_consent is True
    assert "ranked_consent" in RankedEligibilityInputs.__dataclass_fields__


def test_ranked_eligibility_module_has_no_impure_imports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    offenders: list[tuple[int, str]] = []
    forbidden_exact = {"random", "datetime"}
    forbidden_prefixes = ("padrino.db", "padrino.settings")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""]
        else:
            continue
        for module in imported:
            if module in forbidden_exact or module.startswith(forbidden_prefixes):
                offenders.append((node.lineno, module))

    assert not offenders
