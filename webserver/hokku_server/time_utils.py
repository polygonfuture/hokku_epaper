"""Sleep scheduling and human-readable duration formatting.

Renamed from `time.py` to avoid shadowing stdlib `time`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hokku_server.app_config import AppConfig

DEBUG_FAST_REFRESH_SECONDS = 180


def _hhmm_to_minutes(s: str) -> int | None:
    """Parse an 'HHMM' string to minutes-of-day, or None if empty/invalid."""
    s = str(s).strip()
    if not s:
        return None
    s = s.zfill(4)
    try:
        h, m = int(s[:2]), int(s[2:])
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _interval_sleep_seconds(config: AppConfig, now: datetime) -> int:
    """Interval-mode sleep: a flat cadence, optionally confined to an active-hours
    window. Outside the window the frame sleeps until the next window start."""
    base = max(60, int(config.refresh_interval_minutes) * 60)
    start = _hhmm_to_minutes(config.refresh_active_start)
    end = _hhmm_to_minutes(config.refresh_active_end)
    if start is None or end is None or start == end:
        return base  # no valid window → 24/7 interval

    wake = now + timedelta(seconds=base)
    wake_min = wake.hour * 60 + wake.minute
    if start < end:
        inside = start <= wake_min <= end
    else:  # window wraps past midnight (e.g. 2200–0600)
        inside = wake_min >= start or wake_min <= end
    if inside:
        return base

    # Next wake would fall outside active hours — sleep until the next window start.
    candidate = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return max(60, int((candidate - now).total_seconds()))


def calculate_sleep_seconds(config: AppConfig) -> int:
    """Seconds until the next scheduled refresh (system local TZ).

    In debug-fast-refresh mode the schedule is bypassed and a flat 180s is used.
    In interval mode the cadence comes from refresh_interval_minutes (optionally
    confined to an active-hours window). In times mode it's the next configured
    clock time; with no times configured, defaults to 6h.
    """
    if config.debug_fast_refresh:
        return DEBUG_FAST_REFRESH_SECONDS

    now = datetime.now().astimezone()  # system tz

    if config.refresh_mode == "interval":
        return _interval_sleep_seconds(config, now)

    times = config.refresh_image_at_time
    if not times:
        return 21600

    wake_times: list[tuple[int, int]] = []
    for t in times:
        s = str(t).zfill(4)
        wake_times.append((int(s[:2]), int(s[2:])))
    wake_times.sort()

    for h, m in wake_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            return max(60, int((candidate - now).total_seconds()))

    # All today's slots are past — use tomorrow's first slot.
    h, m = wake_times[0]
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    return max(60, int((candidate - now).total_seconds()))


def format_duration_human(minutes: float) -> str:
    if minutes < 0:
        return "0m"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = int(minutes % 60)
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    days = hours / 24
    if days < 30:
        d = int(days)
        h = int(hours % 24)
        return f"{d}d {h}h" if h > 0 else f"{d}d"
    if days < 365:
        mo = int(days / 30)
        d = int(days % 30)
        return f"{mo}mo {d}d" if d > 0 else f"{mo}mo"
    years = int(days / 365)
    mo = int((days % 365) / 30)
    return f"{years}y {mo}mo" if mo > 0 else f"{years}y"
