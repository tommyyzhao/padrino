"""Pure lobby helpers (Wave 9 human multiplayer)."""

from __future__ import annotations

from padrino.core.lobby.autofill import (
    AutoFillAssignment,
    NotEnoughCuratedModelsError,
    autofill_empty_seats,
)
from padrino.core.lobby.model_selection import (
    ModelAutoFillCandidate,
    ModelAutoFillPolicy,
    NotEnoughEligibleModelsError,
    autofill_model_slugs,
)
from padrino.core.lobby.readiness import (
    LobbyReadinessDecision,
    LobbyReadinessMember,
    LobbyReadinessReason,
    LobbyReadinessSeat,
    evaluate_lobby_readiness,
)
from padrino.core.lobby.seating import (
    AiReservationPolicy,
    LobbySeatPlan,
    LobbySeatPlanEntry,
    LobbySeatPlanError,
    LobbySeatPlanReason,
    claim_lowest_available_ai_seat,
    release_member_seat,
)
from padrino.core.lobby.strangers_only import (
    RANKED_REQUIRES_SINGLE_HUMAN_SEAT,
    RankedSeatingViolation,
    assert_strangers_only_ranked,
)

__all__ = [
    "RANKED_REQUIRES_SINGLE_HUMAN_SEAT",
    "AiReservationPolicy",
    "AutoFillAssignment",
    "LobbyReadinessDecision",
    "LobbyReadinessMember",
    "LobbyReadinessReason",
    "LobbyReadinessSeat",
    "LobbySeatPlan",
    "LobbySeatPlanEntry",
    "LobbySeatPlanError",
    "LobbySeatPlanReason",
    "ModelAutoFillCandidate",
    "ModelAutoFillPolicy",
    "NotEnoughCuratedModelsError",
    "NotEnoughEligibleModelsError",
    "RankedSeatingViolation",
    "assert_strangers_only_ranked",
    "autofill_empty_seats",
    "autofill_model_slugs",
    "claim_lowest_available_ai_seat",
    "evaluate_lobby_readiness",
    "release_member_seat",
]
