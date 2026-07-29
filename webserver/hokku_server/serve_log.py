"""Durable, append-only serve-decision log — ground truth for "what the drawer showed vs what
actually served", per frame.

Every real frame serve appends ONE JSON line to ``<cache_dir>/serve_decisions.jsonl`` capturing the
full decision chain so a drawer≠serve mismatch can be proven from the record instead of inferred
from HTTP access logs. Written synchronously under the serve path (one small line per wake — cheap).

Each line:
  {
    "ts": "2026-07-28T19:25:54",         # server local time of the serve
    "screen": "frame-a",             # X-Screen-Name
    "ip": "192.168.1.20",
    "committed_before": "IMG_0001.jpeg",  # what THIS screen's drawer showed (committed pick pre-serve)
    "pin": null,                          # active per-screen pin (next_override), if any
    "chosen": "ae2c...jpeg",             # what serve_binary selected to send
    "served": "ae2c...jpeg",             # what mark_served recorded (== chosen unless a later stage swapped)
    "drawer_matched_serve": false,        # chosen == committed_before OR == pin  → the invariant
    "fleet_before": {"E1003": "...", "frame-a": "IMG_0001.jpeg"},   # ALL screens' committed, pre-serve
    "fleet_after":  {"E1003": "...", "frame-a": "..."}              # ALL screens' committed, post-serve
  }

``fleet_before``/``fleet_after`` make cross-frame reshuffle visible: if screen B's committed value
changes in a line written for screen A's serve, an advance mutated an idle frame's pick.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("hokku_server.serve_log")

_FILENAME = "serve_decisions.jsonl"
# Bound the file so it can't grow unbounded (esp. under debug_fast_refresh, which wakes frames
# every few minutes). At ~400 bytes/line, 5 MB ≈ 12k serves. When the live file exceeds this it
# is rotated to ``<name>.1`` (one previous generation kept), so on-disk use stays ≈ 2× this cap.
# Normal operation (a couple of refreshes/day) never approaches it — this only backstops debug use.
_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()
_path: Path | None = None
_enabled = False


def configure(cache_dir: str | Path, enabled: bool = False) -> None:
    """Point the serve log at ``<cache_dir>/serve_decisions.jsonl`` and set whether it writes.

    Called at startup and again on every config reload, so the ``serve_decision_log_enabled``
    settings toggle takes effect immediately (no restart)."""
    global _path, _enabled
    _path = Path(cache_dir) / _FILENAME
    _enabled = bool(enabled)
    if _enabled:
        logger.info("Serve-decision log: ON — %s (rotates at %.1f MB)",
                    _path, _MAX_BYTES / 1024 / 1024)
    else:
        logger.info("Serve-decision log: OFF (enable in Settings for drawer-vs-serve debugging)")


def is_enabled() -> bool:
    """True when serve decisions are being logged. Callers use this to skip building the log
    entry (per-serve snapshots) entirely when logging is off — keeping 'off' zero-cost."""
    return _enabled


def _rotate_if_needed_locked() -> None:
    """Roll the log to ``<name>.1`` once it passes the cap. Caller holds ``_lock``."""
    if _path is None:
        return
    try:
        if _path.exists() and _path.stat().st_size >= _MAX_BYTES:
            backup = _path.with_suffix(_path.suffix + ".1")
            backup.unlink(missing_ok=True)   # keep only ONE previous generation
            _path.replace(backup)
    except OSError as e:  # pragma: no cover
        logger.warning("serve-decision log rotate failed: %s", e)


def record(entry: dict[str, Any]) -> None:
    """Append one decision entry as a JSON line, and also emit it at INFO so it shows in the
    normal server log. No-op when disabled. Never raises — logging must not break a serve."""
    if not _enabled or _path is None:
        return
    line_summary = (
        "SERVE-DECISION screen=%(screen)s committed_before=%(committed_before)r "
        "pin=%(pin)r chosen=%(chosen)r served=%(served)r matched=%(drawer_matched_serve)s"
    )
    try:
        logger.info(line_summary, entry)
    except Exception:  # pragma: no cover — formatting must never break a serve
        pass
    try:
        with _lock:
            _rotate_if_needed_locked()
            with open(_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:  # pragma: no cover
        logger.warning("serve-decision log write failed: %s", e)
