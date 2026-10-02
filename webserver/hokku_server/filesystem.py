"""Filesystem utilities — crash-safe JSON state.

The three rules here exist because of a real data loss on 2026-09-06 (see
Plans/CRASH_SAFE_STATE_PLAN.md). A power cut left image_manager.json renamed into place but
never flushed, so it came back empty; the server read "no photos", rescanned, and saved that
emptiness over the top. 129 editor crops were destroyed by the SAVE, not by the crash.

  1. A write is not durable until fsync. os.replace is atomic for the rename only.
  2. Keep the previous good copy, so "the current file is bad" is survivable.
  3. A load that fails must NEVER become an authoritative empty state that overwrites.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level references so tests can patch just these bindings without
# touching the global os/time singletons (which would affect background threads).
_replace = os.replace
_sleep = time.sleep
_fsync = os.fsync

BAK_SUFFIX = ".bak"


def atomic_write_json(path: Path, payload: dict, *, keep_previous: bool = True) -> None:
    """Write *payload* as JSON to *path* durably, keeping one previous generation.

    Order matters: write → flush → **fsync** → rotate current to .bak → rename tmp into place.
    Without the fsync, NTFS can commit the rename (metadata, journalled) while the file's
    contents are still only in the page cache (data, not journalled) — so a power cut leaves a
    file that exists and is empty. That is exactly what happened on 2026-09-06.

    On Windows, AV scanners (e.g. Defender) can briefly hold the destination
    file open without FILE_SHARE_DELETE, making os.replace fail with
    PermissionError.  Retries with exponential backoff to ride out the scan
    window; re-raises on the fifth failure.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            _fsync(f.fileno())      # the line whose absence cost 129 crops
        # Rotate the current file aside BEFORE replacing it, so a good copy always exists on
        # disk between the two renames. Best-effort: never block the write on it.
        if keep_previous and path.exists():
            try:
                shutil.copyfile(path, path.with_suffix(path.suffix + BAK_SUFFIX))
            except OSError as e:
                logger.debug("Could not refresh %s%s: %s", path.name, BAK_SUFFIX, e)
        for attempt in range(5):
            try:
                _replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                _sleep(0.05 * (2**attempt))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class StateLoadError(Exception):
    """The store exists but neither it nor its backup could be read.

    Raised instead of returning empty so a caller can never mistake corruption for "the user
    has no data" and then persist that mistake.
    """


def load_json_guarded(path: Path, *, label: str) -> tuple[dict | None, str]:
    """Read a JSON state file, recovering from the .bak if the primary is unreadable.

    Returns ``(data, source)`` where source is one of "primary", "backup" or "missing".
    ``(None, "missing")`` means the file genuinely does not exist yet — a first run.

    Raises StateLoadError when the file EXISTS but cannot be parsed and no usable backup is
    available. In that case NOTHING on disk is touched — see the comment at the raise for
    why quarantining there would quietly undo the whole protection.
    """
    if not path.exists():
        return None, "missing"

    try:
        with open(path) as f:
            return json.load(f), "primary"
    except (json.JSONDecodeError, OSError) as e:
        logger.error("%s is unreadable (%s) — trying %s%s", path.name, e, path.name, BAK_SUFFIX)

    bak = path.with_suffix(path.suffix + BAK_SUFFIX)
    if bak.exists():
        try:
            with open(bak) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("%s is also unreadable: %s", bak.name, e)
        else:
            # Recovery succeeded, so the bad file must move out of the way for the caller to
            # re-save over it. Quarantine (never delete) — it is the only forensic evidence.
            quarantine = path.with_suffix(f"{path.suffix}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
            try:
                shutil.move(str(path), str(quarantine))
                logger.error("Quarantined the unreadable %s as %s", path.name, quarantine.name)
            except OSError as e:
                logger.error("Could not quarantine %s: %s", path.name, e)
            logger.warning(
                "RECOVERED %s from %s — changes since the last save are lost, "
                "but nothing else is", label, bak.name
            )
            return data, "backup"

    # No recovery. Leave EVERY file exactly where it is.
    #
    # Deliberately NOT quarantined here: moving it would make the next start see no file at
    # all, treat that as a first run, rebuild from scratch and save — so the refusal would
    # last exactly one boot and any restart (or an auto-restarting service) would silently
    # destroy the data anyway. Leaving it means the server fails the same way every time,
    # until a human decides what to do.
    raise StateLoadError(
        f"{label}: {path.name} is unreadable and there is no usable {bak.name}.\n"
        f"\n"
        f"Refusing to start. Starting with an empty state would overwrite this file, and\n"
        f"whatever is still recoverable inside it, within seconds.\n"
        f"\n"
        f"To fix, pick ONE:\n"
        f"  1. RESTORE A BACKUP (keeps your edits) — copy the newest file from\n"
        f"     backups/ over {path}\n"
        f"  2. START FRESH (loses per-image edits; photos are untouched) — rename or delete\n"
        f"     {path}, then start again. It will rebuild from the images folder.\n"
        f"\n"
        f"Nothing has been modified. The unreadable file is still at {path}."
    )


# ── dated snapshots ──────────────────────────────────────────────────────────
# Separate from the rolling .bak, and solving a different problem: .bak protects against the
# CURRENT file being bad; these protect against a bad state that has been saved repeatedly.

def snapshot(path: Path, backups_dir: Path, *, store: str, keep: int, monthly: bool = False) -> None:
    """Archive *path* under ``backups_dir/store/<date>.json``, if its content is new.

    Only ever called with a file that has just parsed successfully — never from in-memory
    state — so a corrupt file can't be archived over a good one.

    Deduplicates by content hash against the newest existing snapshot, so days with no real
    change cost nothing. That is what makes a small `keep` reach back a long way: the slots
    hold days on which something ACTUALLY changed.
    """
    try:
        if not path.exists():
            return
        dest_dir = backups_dir / store
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y-%m}" if monthly else f"{datetime.now():%Y-%m-%d}"
        dest = dest_dir / f"{stamp}.json"

        raw = path.read_bytes()
        digest = hashlib.sha1(raw, usedforsecurity=False).digest()

        existing = sorted(dest_dir.glob("*.json"))
        if existing:
            newest = existing[-1]
            try:
                if hashlib.sha1(newest.read_bytes(), usedforsecurity=False).digest() == digest:
                    return                      # nothing changed since the last snapshot
            except OSError:
                pass
        if dest.exists() and dest.read_bytes() == raw:
            return

        dest.write_bytes(raw)
        logger.info("Backed up %s -> %s", path.name, dest.relative_to(backups_dir.parent))

        # retention: ISO names sort chronologically, so oldest-first is a plain sort
        stale = sorted(dest_dir.glob("*.json"))[:-keep] if keep > 0 else []
        for old in stale:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError as e:                        # a backup must never break startup
        logger.warning("Snapshot of %s failed (continuing): %s", path.name, e)


def backups_dir_for(cache_dir: Path) -> Path:
    """Where snapshots live: a sibling of cache/, never inside it.

    cache/ is defined as disposable and is already swept by clear_caches() and the orphan
    scrubber. Precious data does not belong there, even though those two happen to only touch
    cache/images/ today.
    """
    return Path(cache_dir).parent / "backups"
