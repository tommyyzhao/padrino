"""Structured action payload submitted by a seat each phase.

`Action` is the only field of an agent response that drives mechanical game
state transitions. Chat / private_message / memory_update / rationale_summary
are persuasion or diagnostic surface — they are never read by resolvers.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from padrino.core.enums import ActionType


class Action(BaseModel):
    """One mechanical move a seat submits in a phase. Immutable."""

    model_config = ConfigDict(frozen=True)

    type: ActionType
    target: str | None = None
