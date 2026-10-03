"""Unit tests for time_utils: calculate_sleep_seconds and format_duration_human."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from hokku_server.app_config import AppConfig
from hokku_server.time_utils import (
    DEBUG_FAST_REFRESH_SECONDS,
    calculate_sleep_seconds,
    format_duration_human,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(**kwargs) -> AppConfig:
    return replace(AppConfig(), **kwargs)


def _fake_now(hour: int, minute: int, second: int = 0) -> datetime:
    """Return today at the given local time (fixed offset so it is stable)."""
    base = datetime.now().astimezone()
    return base.replace(hour=hour, minute=minute, second=second, microsecond=0)


# ── calculate_sleep_seconds ───────────────────────────────────────────────────


def test_debug_fast_refresh_returns_constant():
    cfg = _cfg(debug_fast_refresh=True)
    assert calculate_sleep_seconds(cfg) == DEBUG_FAST_REFRESH_SECONDS


def test_debug_fast_refresh_ignores_times():
    """debug_fast_refresh overrides even with refresh times configured."""
    cfg = _cfg(debug_fast_refresh=True, refresh_image_at_time=["0600", "1200"])
    assert calculate_sleep_seconds(cfg) == DEBUG_FAST_REFRESH_SECONDS


def test_no_refresh_times_returns_six_hours():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=[])
    assert calculate_sleep_seconds(cfg) == 21600


def test_sleep_seconds_non_negative():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    assert calculate_sleep_seconds(cfg) > 0


def test_sleep_seconds_minimum_sixty():
    """Result must be at least 60 s (enforced by max(60, ...) in the function)."""
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    assert calculate_sleep_seconds(cfg) >= 60


def test_sleep_at_most_one_day():
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600"])
    assert calculate_sleep_seconds(cfg) <= 86_400


def test_future_slot_chosen_over_past():
    """With one slot in the future, result should be the seconds until that slot."""
    # Set now to 10:00 and the slot to 10:30 → expect ~1800 s.
    now = _fake_now(10, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["1030"])
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 1_700 <= result <= 1_860, f"Expected ~1800 s, got {result}"


def test_all_past_slots_wrap_to_tomorrow():
    """When all today's slots are in the past, result must exceed remaining seconds today."""
    # Set now to 23:00; the single slot is 06:00 → next occurrence is ~7 h away.
    now = _fake_now(23, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600"])
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = calculate_sleep_seconds(cfg)
    # 06:00 tomorrow from 23:00 today = 7 h = 25200 s
    assert 25_000 <= result <= 25_400, f"Expected ~25200 s, got {result}"


def test_multiple_slots_picks_nearest_future():
    """With slots at 06:00, 12:00, 18:00 and now=10:00, next slot is 12:00 → ~7200 s."""
    now = _fake_now(10, 0)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=["0600", "1200", "1800"])
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = calculate_sleep_seconds(cfg)
    assert 7_000 <= result <= 7_300, f"Expected ~7200 s, got {result}"


# ── slot-boundary grace (double-refresh fix) ──────────────────────────────────
# A frame's RTC wake drifts (minutes over a multi-hour sleep), so it can wake shortly BEFORE a
# scheduled slot. Previously the sleep was floored to 60 s and the frame re-served the SAME slot
# a minute later — a double e-ink repaint. A wake within _SLOT_GRACE_SECONDS of a slot (either
# side) must now target the NEXT slot, not that one.


def _sleep_at(hour, minute, second=0, times=("0600", "1200", "1800")):
    now = _fake_now(hour, minute, second)
    cfg = _cfg(debug_fast_refresh=False, refresh_image_at_time=list(times))
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = now
        return calculate_sleep_seconds(cfg)


def test_wake_just_before_slot_skips_to_next_slot():
    """Waking 30 s before noon must sleep to 18:00 (~6 h), NOT ~30 s back to noon."""
    result = _sleep_at(11, 59, 30)
    assert 21_000 <= result <= 22_000, f"Expected ~6h to 18:00, got {result}s (double-refresh bug)"


def test_wake_a_few_minutes_before_slot_skips_to_next_slot():
    """Waking 3 min early (typical RTC drift) must skip noon and target 18:00."""
    result = _sleep_at(11, 57, 0)
    assert 21_000 <= result <= 22_000, f"Expected ~6h to 18:00, got {result}s"


def test_wake_on_the_slot_targets_next_slot():
    """Waking exactly at noon targets 18:00 (6 h), unchanged from prior correct behavior."""
    result = _sleep_at(12, 0, 0)
    assert 21_400 <= result <= 21_700, f"Expected ~21600 s, got {result}s"


def test_wake_just_after_slot_targets_next_slot():
    """Waking 2 min after noon still targets 18:00 (the just-passed slot is not re-served)."""
    result = _sleep_at(12, 2, 0)
    assert 21_300 <= result <= 21_600, f"Expected ~6h minus 2min, got {result}s"


def test_mid_gap_wake_unaffected_by_grace():
    """A wake well inside a gap (09:00) targets the normal next slot (12:00, ~3h) — the grace
    window does not perturb non-boundary cases."""
    result = _sleep_at(9, 0, 0)
    assert 10_600 <= result <= 10_900, f"Expected ~10800 s to noon, got {result}s"


# ── interval mode ─────────────────────────────────────────────────────────────


def _interval_sleep_at(hour, minute, minutes, start="", end=""):
    cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=minutes,
               refresh_active_start=start, refresh_active_end=end)
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = _fake_now(hour, minute)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        return calculate_sleep_seconds(cfg)


def test_interval_mode_flat_cadence():
    """Interval mode with no active hours sleeps interval_minutes * 60."""
    cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=120)
    assert calculate_sleep_seconds(cfg) == 7200


def test_interval_mode_minimum_is_one_hour():
    """Anything under an hour (30, 0, negative) is raised to the 1-hour minimum."""
    for minutes in (30, 0, -30):
        cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=minutes)
        assert calculate_sleep_seconds(cfg) == 3600, minutes


def test_interval_mode_maximum_is_one_day():
    cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=5000)
    assert calculate_sleep_seconds(cfg) == 86400


def test_interval_mode_ignores_refresh_times():
    """In interval mode the clock-time list is not consulted."""
    cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=120,
               refresh_image_at_time=("0600", "1200"))
    assert calculate_sleep_seconds(cfg) == 7200


def test_times_mode_ignores_interval_settings():
    """The default specific-times schedule is unaffected by the interval fields."""
    plain = _sleep_at(9, 0, 0)
    cfg = _cfg(refresh_interval_minutes=60, refresh_active_start="0700", refresh_active_end="2300",
               refresh_image_at_time=("0600", "1200", "1800"))
    with patch("hokku_server.time_utils.datetime") as mock_dt:
        mock_dt.now.return_value = _fake_now(9, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert calculate_sleep_seconds(cfg) == plain


def test_debug_fast_refresh_beats_interval_mode():
    cfg = _cfg(refresh_mode="interval", refresh_interval_minutes=120, debug_fast_refresh=True)
    assert calculate_sleep_seconds(cfg) == DEBUG_FAST_REFRESH_SECONDS


def test_interval_active_hours_inside_returns_interval():
    """Active 07:00–23:00; now 12:00, the 13:00 wake is inside → the full interval."""
    assert _interval_sleep_at(12, 0, 60, "0700", "2300") == 3600


def test_interval_active_hours_outside_sleeps_until_start():
    """Active 07:00–23:00; now 02:00 → sleeps until 07:00 (5 h)."""
    assert _interval_sleep_at(2, 0, 60, "0700", "2300") == 18000


def test_interval_last_refresh_skips_to_next_morning():
    """Every 2 h, active 07:00–23:00; now 21:30 → 23:30 is outside, so sleep to 07:00 (9.5 h)."""
    assert _interval_sleep_at(21, 30, 120, "0700", "2300") == int(9.5 * 3600)


def test_interval_active_hours_after_window_wraps_to_next_start():
    """Active 07:00–23:00; now 23:30 → next start is 07:00 tomorrow (7.5 h)."""
    assert _interval_sleep_at(23, 30, 60, "0700", "2300") == int(7.5 * 3600)


def test_interval_active_hours_empty_is_24_7():
    """Empty active hours mean the interval runs around the clock."""
    assert _interval_sleep_at(2, 0, 120) == 7200


def test_interval_active_hours_overnight_window():
    """Overnight window 22:00–06:00; now 23:00, the 00:00 wake is inside → the interval."""
    assert _interval_sleep_at(23, 0, 60, "2200", "0600") == 3600


def test_interval_active_hours_equal_start_end_is_24_7():
    assert _interval_sleep_at(12, 0, 120, "0700", "0700") == 7200


# ── format_duration_human ─────────────────────────────────────────────────────


def test_format_negative_is_zero():
    assert format_duration_human(-5) == "0m"


def test_format_zero():
    assert format_duration_human(0) == "0m"


def test_format_minutes_only():
    assert format_duration_human(45) == "45m"


def test_format_exactly_one_hour():
    assert format_duration_human(60) == "1h"


def test_format_hours_and_minutes():
    assert format_duration_human(90) == "1h 30m"


def test_format_exactly_one_day():
    assert format_duration_human(24 * 60) == "1d"


def test_format_days_and_hours():
    assert format_duration_human(36 * 60) == "1d 12h"


def test_format_exactly_one_month():
    # 30 days in minutes
    assert format_duration_human(30 * 24 * 60) == "1mo"


def test_format_months_and_days():
    # 45 days = 1 month 15 days
    assert format_duration_human(45 * 24 * 60) == "1mo 15d"


def test_format_exactly_one_year():
    # 365 days
    assert format_duration_human(365 * 24 * 60) == "1y"


def test_format_years_and_months():
    # 395 days = 1 year + ~1 month
    assert format_duration_human(395 * 24 * 60).startswith("1y")


def test_format_large_hours_no_minutes_omits_zero_minutes():
    assert format_duration_human(120) == "2h"


def test_format_large_days_no_hours_omits_zero_hours():
    assert format_duration_human(48 * 60) == "2d"
