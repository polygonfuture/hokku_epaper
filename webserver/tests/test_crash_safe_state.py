"""Crash safety for the JSON state stores (Plans/CRASH_SAFE_STATE_PLAN.md).

Written after a real loss on 2026-09-06: a power cut left image_manager.json renamed into
place but never flushed, so it read back empty; the server took that as "no photos", rescanned
the folder, and SAVED the empty state over it. 129 editor crops were destroyed by the save,
not by the crash.

The regression test that matters is test_power_cut_does_not_destroy_edit_crops — it reproduces
that exact sequence.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hokku_server import filesystem
from hokku_server.app_config import AppConfig
from hokku_server.filesystem import (
    BAK_SUFFIX,
    StateLoadError,
    atomic_write_json,
    backups_dir_for,
    load_json_guarded,
    snapshot,
)
from hokku_server.image_manager_single import SingleThreadedImageManager


# ── D1: the write must be durable, and in the right ORDER ────────────────────


def test_fsync_runs_before_the_rename(tmp_path, monkeypatch):
    """os.replace is atomic for the rename only. Without an fsync first, NTFS can commit the
    rename while the contents are still in the page cache — which is how the file came back
    empty. The ORDER is the whole fix, so assert the sequence, not just that fsync happened.
    """
    # Record only events raised on THIS thread: other tests' render pools can be saving their
    # own DBs concurrently, and their interleaved calls would corrupt a global sequence.
    mine = threading.get_ident()
    seq: list[str] = []
    real_fsync, real_replace = filesystem._fsync, filesystem._replace

    def spy_fsync(fd):
        if threading.get_ident() == mine:
            seq.append("fsync")
        return real_fsync(fd)

    def spy_replace(a, b):
        if threading.get_ident() == mine:
            seq.append("replace")
        return real_replace(a, b)

    monkeypatch.setattr(filesystem, "_fsync", spy_fsync)
    monkeypatch.setattr(filesystem, "_replace", spy_replace)

    atomic_write_json(tmp_path / "state.json", {"hello": "world"})
    assert seq == ["fsync", "replace"], f"fsync must precede the rename, got {seq}"


def test_write_keeps_one_previous_generation(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_json(p, {"gen": 1})
    atomic_write_json(p, {"gen": 2})

    assert json.loads(p.read_text())["gen"] == 2
    assert json.loads(p.with_suffix(".json" + BAK_SUFFIX).read_text())["gen"] == 1


def test_no_tmp_file_is_left_behind(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_json(p, {"a": 1})
    assert not list(tmp_path.glob("*.tmp"))


# ── D2: a failed load must never become an authoritative empty state ─────────


def test_missing_file_is_a_first_run_not_an_error(tmp_path):
    data, source = load_json_guarded(tmp_path / "nope.json", label="x")
    assert data is None and source == "missing"


def test_corrupt_file_recovers_from_the_backup(tmp_path):
    p = tmp_path / "state.json"
    atomic_write_json(p, {"good": True})      # creates no .bak yet (nothing to rotate)
    atomic_write_json(p, {"good": True, "n": 2})   # now .bak holds the first write
    p.write_text("")                          # the power-cut signature: present but empty

    data, source = load_json_guarded(p, label="test store")
    assert source == "backup"
    assert data["good"] is True


def test_corrupt_file_is_quarantined_only_when_we_recovered(tmp_path):
    """Quarantine belongs on the RECOVERY path — the bad file must move so the good state can
    be re-saved over it. The evidence is kept, never deleted."""
    p = tmp_path / "state.json"
    atomic_write_json(p, {"n": 1})
    atomic_write_json(p, {"n": 2})            # .bak now holds {"n": 1}
    p.write_text("{ truncated")

    data, source = load_json_guarded(p, label="test store")
    assert source == "backup" and data == {"n": 1}
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ truncated", "evidence must survive verbatim"


def test_refusal_leaves_every_file_untouched(tmp_path):
    """With no backup we refuse — and must NOT move the file aside.

    Quarantining here would make the next start see no file at all, treat it as a first run,
    rebuild and save: the refusal would last exactly one boot, and any restart would destroy
    the data anyway.
    """
    p = tmp_path / "state.json"
    p.write_text("{ truncated")
    before = sorted(x.name for x in tmp_path.iterdir())

    with pytest.raises(StateLoadError):
        load_json_guarded(p, label="image database")

    assert p.exists() and p.read_text() == "{ truncated"
    assert sorted(x.name for x in tmp_path.iterdir()) == before, "nothing may be created or moved"


def test_refusal_is_durable_across_restarts(tmp_path):
    """The protection must not evaporate on the second attempt — a service that auto-restarts
    would otherwise silently rebuild over the data."""
    p = tmp_path / "state.json"
    p.write_text("")
    for attempt in range(3):
        with pytest.raises(StateLoadError):
            load_json_guarded(p, label="image database")


def test_refusal_message_tells_the_user_what_to_do(tmp_path):
    """A refusal the operator can't act on is just an outage."""
    p = tmp_path / "state.json"
    p.write_text("")
    with pytest.raises(StateLoadError) as ei:
        load_json_guarded(p, label="image database")
    msg = str(ei.value)
    assert "backups/" in msg, "must name the restore path"
    assert "rename or delete" in msg, "must offer the start-fresh escape hatch"
    assert str(p) in msg, "must name the actual file"


def test_unreadable_with_no_backup_raises_rather_than_returning_empty(tmp_path):
    """The core lesson. Returning {} here is what let the caller persist the loss."""
    p = tmp_path / "state.json"
    p.write_text("not json")
    with pytest.raises(StateLoadError):
        load_json_guarded(p, label="image database")


# ── the incident, reproduced ─────────────────────────────────────────────────


def test_power_cut_does_not_destroy_edit_crops(app_config: AppConfig, make_test_image):
    """REGRESSION for 2026-09-06: crop a photo, simulate the power cut, restart, keep the crop.

    Before the fix this sequence silently rebuilt the library and lost every crop.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    make_test_image(upload / "b.png")

    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    crop = {"rotation_quarters": 0, "rect": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
            "target": "portrait"}
    mgr.commit_edit("a.png", None, crop)
    mgr.shutdown()

    db = Path(app_config.cache_dir) / "image_manager.json"
    assert json.loads(db.read_text())["images"]["a.png"]["edit_crop"], "crop should be saved"

    db.write_text("")          # the crash: renamed into place, contents never flushed

    mgr2 = SingleThreadedImageManager(app_config)     # the restart
    rec = mgr2.status("a.png")
    assert rec is not None, "the library must not come back empty"
    assert rec.edit_crop == crop, "the editor crop must survive a power cut"
    mgr2.sync()                                       # the step that used to make it permanent
    assert mgr2.status("a.png").edit_crop == crop


def test_unrecoverable_db_refuses_rather_than_rebuilding(app_config: AppConfig, make_test_image):
    """With no usable .bak the manager must refuse, not silently re-register everything.

    A dead server is recoverable; an overwritten one is not.
    """
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    mgr.shutdown()

    cache = Path(app_config.cache_dir)
    (cache / "image_manager.json").write_text("")
    bak = cache / ("image_manager.json" + BAK_SUFFIX)
    if bak.exists():
        bak.unlink()

    with pytest.raises(StateLoadError):
        SingleThreadedImageManager(app_config)


# ── dated snapshots ──────────────────────────────────────────────────────────


def test_snapshot_dedupes_and_prunes(tmp_path):
    """Unchanged content writes nothing — which is what makes a small keep reach back far."""
    src = tmp_path / "state.json"
    backups = tmp_path / "backups"
    src.write_text('{"v": 1}')

    snapshot(src, backups, store="s", keep=3)
    snapshot(src, backups, store="s", keep=3)     # identical -> no second file
    assert len(list((backups / "s").glob("*.json"))) == 1

    # a changed file on an older date, then prune to the cap
    for day, payload in [("2026-01-01", "a"), ("2026-01-02", "b"), ("2026-01-03", "c")]:
        (backups / "s" / f"{day}.json").write_text(payload)
    src.write_text('{"v": 2}')
    snapshot(src, backups, store="s", keep=3)
    kept = sorted(p.name for p in (backups / "s").glob("*.json"))
    assert len(kept) == 3, kept
    assert kept == sorted(kept), "ISO names must sort chronologically for pruning to be correct"


def test_snapshot_never_raises(tmp_path):
    """A backup must never be able to stop the server starting."""
    snapshot(tmp_path / "missing.json", tmp_path / "b", store="s", keep=2)   # no file: no-op


def test_backups_live_outside_the_cache_dir(tmp_path):
    """cache/ is swept by clear_caches() and the orphan scrubber; backups don't belong in it."""
    cache = tmp_path / "hokku" / "cache"
    out = backups_dir_for(cache)
    assert out == tmp_path / "hokku" / "backups"
    assert cache not in out.parents and out != cache
