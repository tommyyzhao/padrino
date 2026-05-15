"""Linter-style assertions over the bundled canonical mini7_v1 prompts (US-052).

These tests guarantee structural properties every contributor must preserve
when editing the bundled markdown files: non-empty, size-capped, role-safe
wording for the citizen prompt, and the explicit "never reveal your role"
clause every role family is supposed to carry.
"""

from __future__ import annotations

import re

import pytest

from padrino.core.enums import RoleFamily
from padrino.core.rulesets import mini7_v1
from padrino.llm.prompts import (
    CANONICAL_VERSION,
    PromptTemplate,
    iter_canonical_prompts,
    load_canonical,
)

_MAX_PROMPT_BYTES = 4096


@pytest.mark.parametrize("role_family", list(RoleFamily))
def test_every_role_family_has_a_canonical_prompt(role_family: RoleFamily) -> None:
    template = load_canonical(mini7_v1.RULESET_ID, role_family)
    assert isinstance(template, PromptTemplate)
    assert template.version == CANONICAL_VERSION
    assert template.role_family is role_family
    assert template.system_prompt.strip(), "prompt body must not be blank"


def test_iter_canonical_prompts_covers_every_role_family() -> None:
    templates = iter_canonical_prompts()
    assert len(templates) == len(RoleFamily)
    seen = {t.role_family for t in templates}
    assert seen == set(RoleFamily)


@pytest.mark.parametrize("role_family", list(RoleFamily))
def test_prompt_size_is_capped_under_4kb(role_family: RoleFamily) -> None:
    template = load_canonical(mini7_v1.RULESET_ID, role_family)
    encoded = template.system_prompt.encode("utf-8")
    assert 0 < len(encoded) < _MAX_PROMPT_BYTES, (
        f"{role_family.value} prompt is {len(encoded)} bytes, exceeds 4 KB cap"
    )


@pytest.mark.parametrize("role_family", list(RoleFamily))
def test_every_prompt_carries_the_reveal_warning(role_family: RoleFamily) -> None:
    template = load_canonical(mini7_v1.RULESET_ID, role_family)
    body = template.system_prompt.lower()
    assert "never reveal your role" in body, (
        f"{role_family.value} prompt missing the 'never reveal your role' clause"
    )


@pytest.mark.parametrize("role_family", list(RoleFamily))
def test_every_prompt_documents_the_action_schema(role_family: RoleFamily) -> None:
    template = load_canonical(mini7_v1.RULESET_ID, role_family)
    body = template.system_prompt
    for required_key in ("public_message", "private_message", "action", "memory_update"):
        assert required_key in body, f"{role_family.value} prompt missing {required_key!r}"


def test_vanilla_town_prompt_does_not_name_special_roles() -> None:
    """The citizen prompt must not leak the special role labels in plain English.

    The villager seat learns its own faction from the observation but has no
    informational advantage over other town seats; the prompt should not
    introduce the literal words ``detective``, ``doctor``, or ``mafia``.
    """
    template = load_canonical(mini7_v1.RULESET_ID, RoleFamily.VANILLA_TOWN)
    body = template.system_prompt
    for forbidden in ("detective", "doctor", "mafia"):
        assert not re.search(rf"\b{forbidden}\b", body, flags=re.IGNORECASE), (
            f"VANILLA_TOWN prompt unexpectedly names {forbidden!r}"
        )


def test_prompt_hashes_are_distinct_per_role_family() -> None:
    hashes = {t.role_family: t.prompt_hash for t in iter_canonical_prompts()}
    assert len(set(hashes.values())) == len(hashes), "prompt hashes collided across role families"
