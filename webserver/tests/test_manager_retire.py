"""retire() — freeze a replaced manager's writes so it can't revert its successor.

AppState.reload() builds the replacement manager (which reads image_manager.json
in its constructor) BEFORE disposing of the old one. If the old manager flushes on
the way out, its stale snapshot lands on top of whatever the new one already owns —
and with a multi-threaded manager the in-flight render callbacks keep firing for a
while after that, each one another chance to write. That is the reload-time DB
clobber; retire() closes it by freezing writes instead of flushing.

shutdown() (process exit, tests) keeps flushing — it just retires afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from hokku_server.app_config import AppConfig


def _db(config: AppConfig) -> dict:
    return json.loads((Path(config.cache_dir) / "image_manager.json").read_text())


def test_retire_freezes_writes_so_a_late_callback_cannot_revert(
    app_config: AppConfig, make_test_image, image_manager_factory
):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")

    old = image_manager_factory(app_config)
    old.sync()
    old.wait_for_idle()

    # The successor: built fresh (reads the DB), then learns about a new image —
    # the state AppState.reload() leaves behind.
    make_test_image(upload / "b.png")
    new = image_manager_factory(app_config)
    new.sync()
    new.wait_for_idle()
    assert set(_db(app_config)["images"]) == {"a.png", "b.png"}

    old.retire()
    old._save_db()  # a render callback landing after retirement

    assert set(_db(app_config)["images"]) == {"a.png", "b.png"}, (
        "the retired manager wrote its stale snapshot over the successor's DB"
    )


def test_shutdown_still_flushes_then_freezes(
    app_config: AppConfig, make_test_image, image_manager_factory
):
    upload = Path(app_config.upload_dir)
    make_test_image(upload / "a.png")

    mgr = image_manager_factory(app_config)
    mgr.sync()
    mgr.wait_for_idle()

    db_path = Path(app_config.cache_dir) / "image_manager.json"
    db_path.unlink()
    mgr.shutdown()
    assert set(_db(app_config)["images"]) == {"a.png"}  # flushed on the way out

    db_path.unlink()
    mgr._save_db()  # frozen afterwards, same as retire()
    assert not db_path.exists()


def test_retire_stops_the_workers(app_config: AppConfig, image_manager_factory):
    """Both concretes implement _stop_workers; the pool one is really shut down."""
    mgr = image_manager_factory(app_config)
    mgr.retire()
    executor = getattr(mgr, "_executor", None)
    if executor is not None:
        assert executor._shutdown
