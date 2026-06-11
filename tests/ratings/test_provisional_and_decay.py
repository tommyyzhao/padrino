"""Tests for US-099: provisional flag, ordinal mapping, and sigma-decay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA
from padrino.ratings.provisional_and_decay import (
    DEFAULT_DECAY_IDLE_DAYS,
    DEFAULT_DECAY_SIGMA_PER_DAY,
    DEFAULT_PROVISIONAL_GAMES,
    ORDINAL_BASE,
    ORDINAL_SCALE,
    apply_decay,
    days_idle,
    is_provisional,
    to_ordinal,
)

# ---------------------------------------------------------------------------
# Provisional thresholding
# ---------------------------------------------------------------------------


def test_is_provisional_zero_games_is_provisional() -> None:
    assert is_provisional(0) is True


def test_is_provisional_below_threshold_is_provisional() -> None:
    assert is_provisional(9, threshold=10) is True


def test_is_provisional_exactly_at_threshold_is_established() -> None:
    assert is_provisional(10, threshold=10) is False


def test_is_provisional_above_threshold_is_established() -> None:
    assert is_provisional(20, threshold=10) is False


def test_is_provisional_uses_default_threshold() -> None:
    assert is_provisional(DEFAULT_PROVISIONAL_GAMES - 1) is True
    assert is_provisional(DEFAULT_PROVISIONAL_GAMES) is False


def test_is_provisional_custom_threshold() -> None:
    assert is_provisional(4, threshold=5) is True
    assert is_provisional(5, threshold=5) is False


# ---------------------------------------------------------------------------
# Ordinal mapping
# ---------------------------------------------------------------------------


def test_to_ordinal_initial_rating_is_base() -> None:
    # INITIAL_MU=25, INITIAL_SIGMA=25/3 → conservative_score=0 → ordinal=ORDINAL_BASE
    ordinal = to_ordinal(INITIAL_MU, INITIAL_SIGMA)
    assert ordinal == ORDINAL_BASE


def test_to_ordinal_positive_conservative_score_above_base() -> None:
    # mu=35, sigma=5 → conservative=20 → ordinal=1000+20*40=1800
    assert to_ordinal(35.0, 5.0) == 1800


def test_to_ordinal_negative_conservative_score_below_base() -> None:
    # mu=15, sigma=10 → conservative=-15 → ordinal=1000-600=400
    assert to_ordinal(15.0, 10.0) == 400


def test_to_ordinal_zero_sigma_equals_mu_scaled() -> None:
    # sigma=0 → conservative=mu → ordinal=ORDINAL_BASE + mu * ORDINAL_SCALE
    expected = round(ORDINAL_BASE + 25.0 * ORDINAL_SCALE)
    assert to_ordinal(25.0, 0.0) == expected


def test_to_ordinal_integer_result() -> None:
    result = to_ordinal(26.0, 6.5)
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# days_idle
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def test_days_idle_never_played_returns_zero() -> None:
    assert days_idle(None, now=_NOW) == 0


def test_days_idle_same_day_returns_zero() -> None:
    last = datetime(2026, 6, 10, 6, 0, 0, tzinfo=UTC)
    assert days_idle(last, now=_NOW) == 0


def test_days_idle_one_day_ago() -> None:
    last = _NOW - timedelta(days=1)
    assert days_idle(last, now=_NOW) == 1


def test_days_idle_five_days_ago() -> None:
    last = _NOW - timedelta(days=5)
    assert days_idle(last, now=_NOW) == 5


def test_days_idle_future_last_game_clamps_to_zero() -> None:
    future = _NOW + timedelta(days=3)
    assert days_idle(future, now=_NOW) == 0


def test_days_idle_naive_last_game_at_from_sqlite() -> None:
    # aiosqlite returns naive datetimes for DateTime(timezone=True) columns;
    # the ladder route passes aware datetime.now(UTC). Must not TypeError.
    naive_last = (_NOW - timedelta(days=4)).replace(tzinfo=None)
    assert days_idle(naive_last, now=_NOW) == 4


def test_days_idle_naive_now_with_aware_last_game_at() -> None:
    naive_now = _NOW.replace(tzinfo=None)
    last = _NOW - timedelta(days=2)
    assert days_idle(last, now=naive_now) == 2


# ---------------------------------------------------------------------------
# apply_decay (sigma inflation)
# ---------------------------------------------------------------------------


def test_apply_decay_no_idle_unchanged() -> None:
    sigma = 8.33
    assert apply_decay(sigma, 0) == sigma


def test_apply_decay_negative_idle_unchanged() -> None:
    sigma = 8.33
    assert apply_decay(sigma, -5) == sigma


def test_apply_decay_30_days_default_rate() -> None:
    # sigma * (1 + 0.05 * 30) = sigma * 2.5
    result = apply_decay(8.0, 30, decay_per_day=DEFAULT_DECAY_SIGMA_PER_DAY)
    assert result == pytest.approx(8.0 * 2.5)


def test_apply_decay_10_days_default_rate() -> None:
    # sigma * (1 + 0.05 * 10) = sigma * 1.5
    result = apply_decay(10.0, 10)
    assert result == pytest.approx(10.0 * 1.5)


def test_apply_decay_custom_rate() -> None:
    # sigma * (1 + 0.1 * 5) = sigma * 1.5
    result = apply_decay(6.0, 5, decay_per_day=0.1)
    assert result == pytest.approx(6.0 * 1.5)


def test_apply_decay_sigma_grows_monotonically_with_idle() -> None:
    sigma = 7.0
    prev = sigma
    for days in range(1, 20):
        inflated = apply_decay(sigma, days)
        assert inflated > prev
        prev = inflated


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


def test_settings_provisional_threshold_default() -> None:
    from padrino.settings import Settings

    s = Settings()
    assert s.padrino_provisional_game_threshold == DEFAULT_PROVISIONAL_GAMES


def test_settings_decay_sigma_per_day_default() -> None:
    from padrino.settings import Settings

    s = Settings()
    assert s.padrino_rating_decay_sigma_per_day == DEFAULT_DECAY_SIGMA_PER_DAY


def test_settings_decay_idle_days_default() -> None:
    from padrino.settings import Settings

    s = Settings()
    assert s.padrino_rating_decay_idle_days == DEFAULT_DECAY_IDLE_DAYS


# ---------------------------------------------------------------------------
# AgentBuild.version model field
# ---------------------------------------------------------------------------


def test_agent_build_version_field_accepts_string() -> None:
    from padrino.db.models import AgentBuild

    build = AgentBuild(
        display_name="test",
        model_config_id=__import__("uuid").uuid4(),
        prompt_version_id=__import__("uuid").uuid4(),
        adapter_version="1.0",
        inference_params={},
        active=True,
        version="v2",
    )
    assert build.version == "v2"
