"""Tests for the retention planner (US-108).

Covers:
- Non-broadcastable games past TTL are selected for deletion.
- Broadcastable games (ratings/replay data) are NEVER selected for deletion.
- Games within TTL are not selected.
- LLM-call payloads past raw_payload_ttl_days are selected for scrubbing.
- Games to delete are removed from llm_calls_to_scrub (CASCADE handles them).
- In-progress games (completed_at=None) are never selected.
- dry_run flag propagates to RetentionPlan.
- Settings expose padrino_raw_payload_ttl_days / padrino_non_broadcastable_game_ttl_days.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from padrino.db.retention import (
    GameRetentionInfo,
    RetentionPolicy,
    plan_retention,
)
from padrino.settings import Settings


def _now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _game(
    *,
    is_broadcastable: bool,
    completed_days_ago: float | None,
) -> GameRetentionInfo:
    now = _now()
    completed_at = (
        (now - timedelta(days=completed_days_ago)) if completed_days_ago is not None else None
    )
    return GameRetentionInfo(
        id=uuid.uuid4(),
        is_broadcastable=is_broadcastable,
        completed_at=completed_at,
    )


POLICY = RetentionPolicy(
    raw_payload_ttl_days=30,
    non_broadcastable_game_ttl_days=7,
)


class TestNonBroadcastableDeletion:
    def test_non_broadcastable_past_ttl_selected(self) -> None:
        g = _game(is_broadcastable=False, completed_days_ago=8)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id in plan.games_to_delete

    def test_non_broadcastable_exactly_at_cutoff_selected(self) -> None:
        g = _game(is_broadcastable=False, completed_days_ago=7)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id in plan.games_to_delete

    def test_non_broadcastable_within_ttl_not_selected(self) -> None:
        g = _game(is_broadcastable=False, completed_days_ago=3)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.games_to_delete

    def test_non_broadcastable_in_progress_not_selected(self) -> None:
        g = _game(is_broadcastable=False, completed_days_ago=None)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.games_to_delete
        assert g.id not in plan.llm_calls_to_scrub


class TestBroadcastableProtection:
    """Broadcastable games (ratings/replay data) must never be hard-deleted."""

    def test_broadcastable_past_ttl_not_in_games_to_delete(self) -> None:
        g = _game(is_broadcastable=True, completed_days_ago=90)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.games_to_delete

    def test_broadcastable_only_in_scrub_when_payload_ttl_exceeded(self) -> None:
        g = _game(is_broadcastable=True, completed_days_ago=31)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.games_to_delete
        assert g.id in plan.llm_calls_to_scrub

    def test_broadcastable_not_in_scrub_when_within_payload_ttl(self) -> None:
        g = _game(is_broadcastable=True, completed_days_ago=10)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.games_to_delete
        assert g.id not in plan.llm_calls_to_scrub


class TestLlmCallScrubbing:
    def test_all_completed_games_past_payload_ttl_scrubbed(self) -> None:
        bc = _game(is_broadcastable=True, completed_days_ago=35)
        non_bc = _game(is_broadcastable=False, completed_days_ago=35)
        plan = plan_retention([bc, non_bc], POLICY, now=_now())
        # broadcastable: only scrub (not delete)
        assert bc.id in plan.llm_calls_to_scrub
        assert bc.id not in plan.games_to_delete
        # non-broadcastable: delete (cascade handles llm_calls, so NOT in scrub list)
        assert non_bc.id in plan.games_to_delete
        assert non_bc.id not in plan.llm_calls_to_scrub

    def test_delete_games_excluded_from_scrub_list(self) -> None:
        g = _game(is_broadcastable=False, completed_days_ago=40)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id in plan.games_to_delete
        assert g.id not in plan.llm_calls_to_scrub

    def test_within_payload_ttl_not_scrubbed(self) -> None:
        g = _game(is_broadcastable=True, completed_days_ago=20)
        plan = plan_retention([g], POLICY, now=_now())
        assert g.id not in plan.llm_calls_to_scrub


class TestMixed:
    def test_mixed_batch(self) -> None:
        old_non_bc = _game(is_broadcastable=False, completed_days_ago=10)
        young_non_bc = _game(is_broadcastable=False, completed_days_ago=2)
        old_bc = _game(is_broadcastable=True, completed_days_ago=40)
        fresh_bc = _game(is_broadcastable=True, completed_days_ago=5)
        in_progress = _game(is_broadcastable=False, completed_days_ago=None)

        plan = plan_retention(
            [old_non_bc, young_non_bc, old_bc, fresh_bc, in_progress],
            POLICY,
            now=_now(),
        )

        assert old_non_bc.id in plan.games_to_delete
        assert young_non_bc.id not in plan.games_to_delete
        assert old_bc.id not in plan.games_to_delete
        assert fresh_bc.id not in plan.games_to_delete
        assert in_progress.id not in plan.games_to_delete

        assert old_bc.id in plan.llm_calls_to_scrub
        assert old_non_bc.id not in plan.llm_calls_to_scrub  # deleted by cascade
        assert fresh_bc.id not in plan.llm_calls_to_scrub
        assert young_non_bc.id not in plan.llm_calls_to_scrub
        assert in_progress.id not in plan.llm_calls_to_scrub

    def test_empty_game_list(self) -> None:
        plan = plan_retention([], POLICY, now=_now())
        assert plan.games_to_delete == []
        assert plan.llm_calls_to_scrub == []


class TestDryRunFlag:
    def test_dry_run_default_is_true(self) -> None:
        plan = plan_retention([], POLICY, now=_now())
        assert plan.dry_run is True

    def test_dry_run_false_propagates(self) -> None:
        plan = plan_retention([], POLICY, now=_now(), dry_run=False)
        assert plan.dry_run is False


class TestNaiveDatetimeHandled:
    def test_naive_completed_at_treated_as_utc(self) -> None:
        naive_now = datetime(2026, 6, 10, 12, 0, 0)  # no tzinfo
        g = GameRetentionInfo(
            id=uuid.uuid4(),
            is_broadcastable=False,
            completed_at=datetime(2026, 6, 1, 0, 0, 0),  # 9 days before naive_now, naive
        )
        plan = plan_retention([g], POLICY, now=naive_now)
        assert g.id in plan.games_to_delete


class TestSettings:
    def test_default_raw_payload_ttl_days(self) -> None:
        s = Settings()
        assert s.padrino_raw_payload_ttl_days == 30

    def test_default_non_broadcastable_game_ttl_days(self) -> None:
        s = Settings()
        assert s.padrino_non_broadcastable_game_ttl_days == 7
