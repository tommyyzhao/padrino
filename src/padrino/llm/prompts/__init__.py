"""Canonical mini7_v1 prompts and per-role-family loader (US-052).

Each :class:`~padrino.core.enums.RoleFamily` ships with a hand-authored
markdown prompt under ``padrino/llm/prompts/<ruleset_id>/<role_family>.md``.
The bytes are read once via :mod:`importlib.resources` and cached in process
so the adapter never touches disk per LLM call.

The canonical version string ``canonical_mini7_v1`` is the sentinel an
``AgentBuild`` references when it wants the runtime to pick the prompt by
``(ruleset_id, role_family)`` at game start. Any build that pins a specific
``prompt_version`` other than this sentinel keeps that explicit prompt for
every seat regardless of role family.

Pure helper module: no DB / LLM / clock / network access.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from typing import Final

from pydantic import BaseModel, ConfigDict

from padrino.core.enums import Role, RoleFamily
from padrino.core.rulesets import mini7_v1 as _mini7_ruleset

CANONICAL_VERSION: Final[str] = "canonical_mini7_v1"

# Canonical response schema bundled alongside the prompt rows. Mirrors
# `padrino.core.agents.contract.AgentResponse` — JSON-stable, no datetimes.
CANONICAL_RESPONSE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "public_message",
        "private_message",
        "action",
        "memory_update",
        "rationale_summary",
    ],
    "properties": {
        "public_message": {"type": ["string", "null"]},
        "private_message": {"type": ["string", "null"]},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "target"],
            "properties": {
                "type": {"type": "string"},
                "target": {"type": ["string", "null"]},
            },
        },
        "memory_update": {"type": "string"},
        "rationale_summary": {"type": ["string", "null"]},
    },
}


class PromptTemplate(BaseModel):
    """Value-object projection of one bundled canonical prompt."""

    model_config = ConfigDict(frozen=True)

    ruleset_id: str
    role_family: RoleFamily
    version: str
    system_prompt: str
    prompt_hash: str


def _bundled_dir(ruleset_id: str) -> str:
    if ruleset_id != _mini7_ruleset.RULESET_ID:
        raise UnknownCanonicalPromptError(
            f"no canonical prompts bundled for ruleset_id={ruleset_id!r}"
        )
    return ruleset_id


class UnknownCanonicalPromptError(LookupError):
    """Raised when no canonical prompt is bundled for the requested key."""


@lru_cache
def _read_prompt_text(ruleset_id: str, role_family: RoleFamily) -> str:
    package = f"padrino.llm.prompts.{_bundled_dir(ruleset_id)}"
    filename = f"{role_family.value}.md"
    try:
        return resources.files(package).joinpath(filename).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise UnknownCanonicalPromptError(
            f"missing bundled prompt: package={package!r} file={filename!r}"
        ) from exc


def _prompt_hash(ruleset_id: str, role_family: RoleFamily, system_prompt: str) -> str:
    """Stable sha256 over (ruleset, role_family, content) for DB uniqueness."""
    h = hashlib.sha256()
    h.update(CANONICAL_VERSION.encode("utf-8"))
    h.update(b"|")
    h.update(ruleset_id.encode("utf-8"))
    h.update(b"|")
    h.update(role_family.value.encode("utf-8"))
    h.update(b"|")
    h.update(system_prompt.encode("utf-8"))
    return h.hexdigest()


def load_canonical(ruleset_id: str, role_family: RoleFamily) -> PromptTemplate:
    """Return the canonical :class:`PromptTemplate` for one ``(ruleset, role_family)``.

    The markdown file is read at most once per process. Raises
    :class:`UnknownCanonicalPromptError` if no prompt is bundled.
    """
    text = _read_prompt_text(ruleset_id, role_family)
    return PromptTemplate(
        ruleset_id=ruleset_id,
        role_family=role_family,
        version=CANONICAL_VERSION,
        system_prompt=text,
        prompt_hash=_prompt_hash(ruleset_id, role_family, text),
    )


def iter_canonical_prompts(
    ruleset_id: str = _mini7_ruleset.RULESET_ID,
) -> tuple[PromptTemplate, ...]:
    """Return every canonical prompt for a ruleset in a stable order."""
    return tuple(load_canonical(ruleset_id, rf) for rf in RoleFamily)


def canonical_prompts_by_role(
    ruleset_id: str = _mini7_ruleset.RULESET_ID,
) -> Mapping[Role, str]:
    """Project canonical prompts into a ``Role → system_prompt`` mapping.

    Useful for handing the LiteLLM adapter a per-seat prompt without making
    the adapter import a specific ruleset module.
    """
    out: dict[Role, str] = {}
    for role in Role:
        role_family = _mini7_ruleset.role_family_for(role)
        out[role] = load_canonical(ruleset_id, role_family).system_prompt
    return out


__all__ = [
    "CANONICAL_RESPONSE_SCHEMA",
    "CANONICAL_VERSION",
    "PromptTemplate",
    "UnknownCanonicalPromptError",
    "canonical_prompts_by_role",
    "iter_canonical_prompts",
    "load_canonical",
]
