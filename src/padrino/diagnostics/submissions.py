"""Submission-event vocabulary for invalid-action diagnostics.

These constants define the read-side denominator for timeout and invalid-action
rates. They intentionally include failure events as attempts, plus every
structured action submission event the runner can emit, so diagnostics stay
consistent across rulesets as the action space grows.
"""

from __future__ import annotations

from typing import Final

PUBLIC_MESSAGE_EVENT_TYPE: Final[str] = "PublicMessageSubmitted"
PRIVATE_MESSAGE_EVENT_TYPE: Final[str] = "PrivateMessageSubmitted"
TIMEOUT_EVENT_TYPE: Final[str] = "ActionTimedOut"
INVALID_EVENT_TYPE: Final[str] = "OutputInvalid"
TRUNCATED_EVENT_TYPE: Final[str] = "OutputTruncated"

SUBMISSION_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        PUBLIC_MESSAGE_EVENT_TYPE,
        PRIVATE_MESSAGE_EVENT_TYPE,
        "VoteSubmitted",
        "MafiaKillVoteSubmitted",
        "ProtectSubmitted",
        "InvestigateSubmitted",
        "RoleblockSubmitted",
        "FrameSubmitted",
        "TrackSubmitted",
        "WatchSubmitted",
        "CleanSubmitted",
        "SerialKillSubmitted",
        TIMEOUT_EVENT_TYPE,
        INVALID_EVENT_TYPE,
        TRUNCATED_EVENT_TYPE,
    }
)

__all__ = [
    "INVALID_EVENT_TYPE",
    "PRIVATE_MESSAGE_EVENT_TYPE",
    "PUBLIC_MESSAGE_EVENT_TYPE",
    "SUBMISSION_EVENT_TYPES",
    "TIMEOUT_EVENT_TYPE",
    "TRUNCATED_EVENT_TYPE",
]
