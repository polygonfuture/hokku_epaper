"""Background poll loop: periodically calls ImageManager.sync()."""

from __future__ import annotations

import logging
import threading

from hokku_server.app_state import AppState
from hokku_server.image_record import ConvertStatus

logger = logging.getLogger(__name__)


class Watcher:
    """Sync the current AppState's manager on a fixed cadence.

    Reads ``state.manager`` on every tick so it automatically follows a hot
    reload — after ``AppState.reload()`` swaps in a new manager, the next
    iteration syncs the new one without any restart.

    The daemon thread starts immediately on construction.  Call :meth:`wake`
    at any time to interrupt the current sleep and trigger a sync right away
    (e.g. after a config change).  Call :meth:`stop` to signal the thread to
    exit cleanly after its current sync completes::

        watcher = Watcher(state)          # thread running, first sync in progress
        # … config reloaded …
        watcher.wake()                    # skip remaining sleep, sync now
        watcher.stop()                    # signal clean shutdown
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._run = True
        self._wake = threading.Event()
        #: Names of ready (converted) images at the end of the last sync. The scheduler
        #: only commits an "up next" on startup / serve / pin / config change / delete —
        #: nothing fires when a CONVERSION finishes. So a server that starts with the
        #: library invalidated (any cache-busting settings change) finds no ready images,
        #: commits nothing, and the drawer shows no "up next" until a frame next wakes,
        #: which can be hours. Diffing this set turns "the library finished rendering"
        #: into the missing trigger.
        self._ready_names: set[str] | None = None
        threading.Thread(
            target=self.run_forever,
            daemon=True,
            name="watcher",
        ).start()

    def wake(self) -> None:
        """Interrupt the current sleep and trigger a sync immediately."""
        self._wake.set()

    def stop(self) -> None:
        """Signal the thread to exit after its current sync completes."""
        self._run = False
        self._wake.set()  # interrupt sleep so the exit is prompt

    def run_forever(self) -> None:
        while self._run:
            manager = self._state.manager
            try:
                manager.sync()
                self._recommit_if_library_changed()
            except Exception:
                logger.exception("Watcher sync error")
            self._wake.wait(timeout=manager.config.poll_interval_seconds)
            self._wake.clear()

    def _recommit_if_library_changed(self) -> None:
        """Tell the scheduler when the set of servable images changed.

        Covers conversions completing, uploads, and deletions alike. ``library_changed``
        is a passive recommit — a frame keeps its existing valid pick, so this never
        disturbs a drawer that is already correct; it only fills screens that have none.
        """
        ready = {
            r.name
            for r in self._state.manager.list()
            if r.convert_status == ConvertStatus.OK
        }
        if ready == self._ready_names:
            return
        first_pass = self._ready_names is None
        self._ready_names = ready
        if first_pass:
            return  # startup already commits; don't repeat it before anything changed
        try:
            self._state.scheduler.library_changed()
        except Exception:
            logger.exception("Watcher recommit error")
