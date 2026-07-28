"""Sleep scheduling and human-readable duration formatting.

Renamed from `time.py` to avoid shadowing stdlib `time`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hokku_server.app_config import AppConfig

DEBUG_FAST_REFRESH_SECONDS = 180

# A wake landing within this window of a scheduled slot (just before OR just after) is treated
# as THIS refresh being that slot — so we schedule the NEXT slot rather than a tiny sleep back
# to the same one. Frames wake via an RTC timer that drifts (~1-2% over a multi-hour sleep =
# minutes), so a wake a few minutes early is normal. Without this, a frame waking a bit before a
# slot was told to sleep the few-minutes-to-the-slot (floored to 60s), then woke again and served
# the SAME slot a second time — a wasteful double e-ink repaint + an extra rotation step. The
# window must be comfortably larger than expected drift yet far smaller than the gap between slots
# (default slots are 6h apart), so it can never swallow a genuinely-separate slot. 5 min.
_SLOT_GRACE_SECONDS = 300


def calculate_sleep_seconds(config: AppConfig) -> int:
    """Seconds until the next configured refresh time (system local TZ).

    In debug-fast-refresh mode the schedule is bypassed and a flat 180s is
    used instead. With no times configured, defaults to 6h.

    A slot within ``_SLOT_GRACE_SECONDS`` of *now* (either side) counts as the slot this wake is
    serving; the sleep targets the FIRST slot that is more than that grace ahead — so a frame that
    wakes a little early doesn't refresh the same slot twice.
    """
    if config.debug_fast_refresh:
        return DEBUG_FAST_REFRESH_SECONDS

    now = datetime.now().astimezone()  # system tz
    times = config.refresh_image_at_time
    if not times:
        return 21600

    wake_times: list[tuple[int, int]] = []
    for t in times:
        s = str(t).zfill(4)
        wake_times.append((int(s[:2]), int(s[2:])))
    wake_times.sort()

    # Consider today's remaining slots and tomorrow's first, and take the earliest one that is
    # more than the grace window in the future. A slot within the grace (imminent or just passed)
    # is the slot this wake is serving, so we skip past it.
    candidates: list[datetime] = [
        now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in wake_times
    ]
    h0, m0 = wake_times[0]
    candidates.append(
        (now + timedelta(days=1)).replace(hour=h0, minute=m0, second=0, microsecond=0)
    )

    for candidate in candidates:
        secs = (candidate - now).total_seconds()
        if secs > _SLOT_GRACE_SECONDS:
            return int(secs)

    # Fallback (shouldn't be reached — tomorrow's first slot is always >grace ahead): next day.
    tomorrow = now + timedelta(days=1)
    candidate = tomorrow.replace(hour=h0, minute=m0, second=0, microsecond=0)
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
