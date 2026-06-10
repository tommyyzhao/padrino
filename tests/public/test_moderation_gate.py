"""Unit tests for the moderation gate (US-093).

Covers:
- Clean transcript -> broadcastable (mock guard passes)
- Transcript with seeded toxic line -> not broadcastable (first-pass fails)
- Guard error -> not broadcastable (fail-closed)
- Guard unavailable (None) -> not broadcastable (fail-closed)
- Guard returning False -> not broadcastable
- Events with no public messages pass first-pass
"""

from __future__ import annotations

from padrino.public.moderation import (
    GuardModelAdapter,
    deterministic_first_pass,
    is_broadcastable,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _PassingGuard:
    async def check(self, messages: list[str]) -> bool:
        return True


class _FailingGuard:
    async def check(self, messages: list[str]) -> bool:
        return False


class _ErrorGuard:
    async def check(self, messages: list[str]) -> bool:
        raise RuntimeError("guard model unavailable")


def _public_event(text: str) -> dict[str, object]:
    return {"event_type": "PublicMessage", "payload": {"text": text}}


def _non_chat_event() -> dict[str, object]:
    return {"event_type": "PhaseChanged", "payload": {"phase": "NIGHT"}}


# ---------------------------------------------------------------------------
# GuardModelAdapter protocol check
# ---------------------------------------------------------------------------


def test_passing_guard_satisfies_protocol() -> None:
    assert isinstance(_PassingGuard(), GuardModelAdapter)


# ---------------------------------------------------------------------------
# deterministic_first_pass
# ---------------------------------------------------------------------------


def test_clean_message_passes() -> None:
    events = [_public_event("Hello, I think we should vote for player 3.")]
    assert deterministic_first_pass(events) is True


def test_toxic_sentinel_fails() -> None:
    events = [_public_event("This contains a toxic_word in it.")]
    assert deterministic_first_pass(events) is False


def test_kill_yourself_fails() -> None:
    events = [_public_event("kill yourself mate")]
    assert deterministic_first_pass(events) is False


def test_case_insensitive_match() -> None:
    events = [_public_event("TOXIC_WORD right here")]
    assert deterministic_first_pass(events) is False


def test_empty_events_passes() -> None:
    assert deterministic_first_pass([]) is True


def test_non_chat_events_ignored() -> None:
    events = [_non_chat_event()]
    assert deterministic_first_pass(events) is True


def test_mixed_events_toxic_fails() -> None:
    events = [_non_chat_event(), _public_event("toxic_word appears here")]
    assert deterministic_first_pass(events) is False


def test_multiple_clean_messages_pass() -> None:
    events = [
        _public_event("Good morning everyone."),
        _public_event("I suspect player 2 is mafia."),
    ]
    assert deterministic_first_pass(events) is True


# ---------------------------------------------------------------------------
# is_broadcastable — fail-closed paths
# ---------------------------------------------------------------------------


async def test_guard_none_not_broadcastable() -> None:
    events = [_public_event("Totally clean message.")]
    assert await is_broadcastable(events, None) is False


async def test_guard_error_not_broadcastable() -> None:
    events = [_public_event("Totally clean message.")]
    assert await is_broadcastable(events, _ErrorGuard()) is False


async def test_guard_failing_not_broadcastable() -> None:
    events = [_public_event("Totally clean message.")]
    assert await is_broadcastable(events, _FailingGuard()) is False


# ---------------------------------------------------------------------------
# is_broadcastable — happy path
# ---------------------------------------------------------------------------


async def test_clean_transcript_broadcastable() -> None:
    events = [
        _public_event("Hello everyone, let's discuss."),
        _public_event("I vote for player 4."),
    ]
    assert await is_broadcastable(events, _PassingGuard()) is True


async def test_empty_events_broadcastable_with_passing_guard() -> None:
    assert await is_broadcastable([], _PassingGuard()) is True


# ---------------------------------------------------------------------------
# is_broadcastable — first-pass catches toxic before guard is called
# ---------------------------------------------------------------------------


async def test_toxic_line_fails_before_guard() -> None:
    """First-pass should reject; guard is never consulted."""
    events = [_public_event("toxic_word is banned.")]
    # Even with a passing guard, the first-pass should cause rejection.
    assert await is_broadcastable(events, _PassingGuard()) is False


async def test_toxic_line_with_error_guard_still_fails() -> None:
    events = [_public_event("toxic_word here too")]
    # First-pass failure means the error guard is never called.
    assert await is_broadcastable(events, _ErrorGuard()) is False


# ---------------------------------------------------------------------------
# Settings smoke-test
# ---------------------------------------------------------------------------


def test_guard_model_setting_has_deepinfra_default() -> None:
    from padrino.settings import Settings

    s = Settings()
    assert "deepinfra" in s.padrino_guard_model
