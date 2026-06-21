"""Anonymity-guard property scaffold (Wave 9, US-124).

The two non-negotiable Wave-9 invariants begin here. This module guards the
ANONYMITY half: no human-vs-AI or model-identity marker may reach an
observation / public / spectator frame before the endgame reveal, and the guard
must catch a new identity DB COLUMN, not only a forbidden payload key.

The checks here run in the DEFAULT suite (no ``integration`` marker) and are
promoted to a comprehensive CI gate by US-146.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from padrino.core.observation_privacy import (
    ANONYMOUS,
    FORBIDDEN_PAYLOAD_KEYS,
    HUMAN_IDENTITY_KEYS,
    PUBLIC_GAME_FIELDS,
    PUBLIC_SEAT_FIELDS,
    TRANSPARENT,
    AnonymityViolation,
    assert_anonymous_safe,
    coerce_identity_mode,
    is_anonymous,
    project_game_row,
    project_row_through_allowlist,
    project_seat_row,
)

# --------------------------------------------------------------------------- #
# The human-identity marker set is wired into the deny list
# --------------------------------------------------------------------------- #

_EXPECTED_HUMAN_KEYS = {
    "is_human",
    "controller_type",
    "seat_kind",
    "occupant_principal_id",
    "occupant_user_id",
    "human_player_id",
    "takeover",
    "taken_over_at_phase",
    "takeover_agent_build_id",
}


def test_human_identity_keys_match_the_story_set() -> None:
    assert HUMAN_IDENTITY_KEYS == _EXPECTED_HUMAN_KEYS


def test_human_identity_keys_are_in_the_forbidden_payload_keys() -> None:
    assert HUMAN_IDENTITY_KEYS <= FORBIDDEN_PAYLOAD_KEYS


def test_spectator_projection_alias_inherits_human_keys() -> None:
    # The spectator deny-list is an alias of FORBIDDEN_PAYLOAD_KEYS, so the
    # human markers must be covered there too (no drift).
    from padrino.core.spectator_projection import SPECTATOR_FORBIDDEN_PAYLOAD_KEYS

    assert HUMAN_IDENTITY_KEYS <= SPECTATOR_FORBIDDEN_PAYLOAD_KEYS


# --------------------------------------------------------------------------- #
# assert_anonymous_safe: raises on any forbidden key (incl. human markers)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(FORBIDDEN_PAYLOAD_KEYS))
def test_assert_anonymous_safe_raises_on_top_level_forbidden_key(key: str) -> None:
    with pytest.raises(AnonymityViolation):
        assert_anonymous_safe({key: "anything", "text": "hi"})


@pytest.mark.parametrize("key", sorted(HUMAN_IDENTITY_KEYS))
def test_assert_anonymous_safe_raises_on_nested_human_marker(key: str) -> None:
    frame = {"seats": [{"public_player_id": "P01", "meta": {key: True}}]}
    with pytest.raises(AnonymityViolation):
        assert_anonymous_safe(frame)


def test_assert_anonymous_safe_allows_a_clean_frame() -> None:
    frame = {
        "public_player_id": "P01",
        "seats": [{"public_player_id": "P02", "alive": True}],
        "phase": "DAY_1",
        "tally": {"P01": 2},
    }
    assert_anonymous_safe(frame)  # does not raise


_human_keys = st.sampled_from(sorted(HUMAN_IDENTITY_KEYS))
_plain_keys = st.sampled_from(["text", "target", "phase", "tally", "public_player_id", "nested"])
_scalars = st.one_of(st.text(max_size=8), st.integers(), st.booleans(), st.none())


def _clean_frames() -> st.SearchStrategy[Any]:
    return st.recursive(
        _scalars,
        lambda children: st.one_of(
            st.dictionaries(_plain_keys, children, max_size=4),
            st.lists(children, max_size=4),
        ),
        max_leaves=12,
    ).filter(lambda v: isinstance(v, dict))


def _frames_with_a_human_marker() -> st.SearchStrategy[Any]:
    return st.builds(
        lambda base, key: {**base, key: True},
        _clean_frames(),
        _human_keys,
    )


@given(frame=_clean_frames())
def test_clean_frames_never_raise(frame: dict[str, Any]) -> None:
    assert_anonymous_safe(frame)


@given(frame=_frames_with_a_human_marker())
def test_any_human_marker_anywhere_raises(frame: dict[str, Any]) -> None:
    with pytest.raises(AnonymityViolation):
        assert_anonymous_safe(frame)


# --------------------------------------------------------------------------- #
# Column-level allowlist guard: a NEW identity column cannot leak
# --------------------------------------------------------------------------- #


def test_seat_row_projection_drops_unlisted_identity_columns() -> None:
    # A seat row carrying every human-identity column plus a hypothetical
    # *future* identity column not even in the deny list.
    row = {
        "public_player_id": "P01",
        "seat_index": 0,
        "alive": True,
        "death_phase": None,
        "seat_kind": "HUMAN",
        "occupant_principal_id": "pr_123",
        "is_human": True,
        "takeover_agent_build_id": "ab_9",
        "a_future_identity_column": "leak-me",  # not a payload key, not allowlisted
    }
    projected = project_seat_row(row)
    assert set(projected) == {"public_player_id", "seat_index", "alive", "death_phase"}
    assert "a_future_identity_column" not in projected
    assert_anonymous_safe(projected)


def test_game_row_projection_drops_identity_mode_and_unknowns() -> None:
    row = {
        "public_id": "g_1",
        "ruleset_id": "mini7_v1",
        "status": "LIVE",
        "identity_mode": "ANONYMOUS",
        "host_principal_id": "pr_1",
        "secret_future_col": "x",
    }
    projected = project_game_row(row)
    assert set(projected) <= PUBLIC_GAME_FIELDS
    assert "identity_mode" not in projected
    assert "host_principal_id" not in projected
    assert "secret_future_col" not in projected


def test_allowlist_guard_fail_closed_on_a_mistakenly_allowlisted_forbidden_key() -> None:
    # If a forbidden key were ever added to an allowlist by mistake, the
    # ANONYMOUS-mode post-check still catches it.
    bad_allowlist = PUBLIC_SEAT_FIELDS | {"seat_kind"}
    row = {"public_player_id": "P01", "seat_kind": "HUMAN"}
    with pytest.raises(AnonymityViolation):
        project_row_through_allowlist(row, bad_allowlist, identity_mode=ANONYMOUS)


# --------------------------------------------------------------------------- #
# Fail closed: missing / None identity_mode coerces to ANONYMOUS, never TRANSPARENT
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mode",
    [None, "", "  ", "anon", "anonymous", "ANON", "nonsense", 0, 1, object()],
)
def test_unknown_or_missing_mode_coerces_to_anonymous(mode: Any) -> None:
    assert coerce_identity_mode(mode) == ANONYMOUS
    assert is_anonymous(mode)


@pytest.mark.parametrize("mode", ["TRANSPARENT", "transparent", "  Transparent  "])
def test_only_explicit_transparent_opts_out(mode: str) -> None:
    assert coerce_identity_mode(mode) == TRANSPARENT
    assert not is_anonymous(mode)


def test_enum_like_value_attribute_is_honored() -> None:
    class _Mode:
        value = "TRANSPARENT"

    assert coerce_identity_mode(_Mode()) == TRANSPARENT


def test_seat_row_projection_strips_when_mode_missing() -> None:
    # No identity_mode passed -> fail closed -> ANONYMOUS post-check runs.
    row = {"public_player_id": "P01", "seat_index": 0, "alive": True, "death_phase": None}
    assert project_seat_row(row) == row
