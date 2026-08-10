"""The drawer's "up next" must appear once the library finishes converting.

The scheduler commits a per-screen next pick on startup, serve, pin, config change and
delete — but nothing fires when a CONVERSION completes. A server that starts with the
library invalidated (any cache-busting settings change) therefore finds no ready images
at startup, commits nothing, and shows an empty "up next" until a frame next wakes —
hours, for sleeping e-ink frames. The watcher closes that gap by diffing the ready set.
"""

from __future__ import annotations

from hokku_server.watcher import Watcher


class _Rec:
    def __init__(self, name, status):
        self.name = name
        self.convert_status = status


class _Manager:
    def __init__(self, records):
        self.records = records

    def list(self):
        return self.records


class _Scheduler:
    def __init__(self):
        self.calls = 0

    def library_changed(self):
        self.calls += 1


class _State:
    def __init__(self, manager, scheduler):
        self.manager = manager
        self.scheduler = scheduler


def _watcher(state) -> Watcher:
    """A Watcher without its background thread — we drive the hook directly."""
    w = Watcher.__new__(Watcher)
    w._state = state
    w._ready_names = None
    return w


def test_first_pass_does_not_recommit():
    """Startup already commits; the watcher must not immediately repeat it."""
    sched = _Scheduler()
    w = _watcher(_State(_Manager([_Rec("a.jpg", "ok")]), sched))
    w._recommit_if_library_changed()
    assert sched.calls == 0


def test_recommits_when_conversions_finish():
    """The real case: nothing ready at startup, everything ready a sync later."""
    mgr = _Manager([_Rec("a.jpg", "pending"), _Rec("b.jpg", "pending")])
    sched = _Scheduler()
    w = _watcher(_State(mgr, sched))

    w._recommit_if_library_changed()          # startup: nothing ready
    assert sched.calls == 0

    mgr.records = [_Rec("a.jpg", "ok"), _Rec("b.jpg", "ok")]
    w._recommit_if_library_changed()          # renders finished
    assert sched.calls == 1


def test_no_recommit_while_the_ready_set_is_unchanged():
    """Idle polling must not churn the scheduler (it would fight minimal-disturbance)."""
    mgr = _Manager([_Rec("a.jpg", "ok")])
    sched = _Scheduler()
    w = _watcher(_State(mgr, sched))
    for _ in range(5):
        w._recommit_if_library_changed()
    assert sched.calls == 0


def test_recommits_on_delete_too():
    mgr = _Manager([_Rec("a.jpg", "ok"), _Rec("b.jpg", "ok")])
    sched = _Scheduler()
    w = _watcher(_State(mgr, sched))
    w._recommit_if_library_changed()
    mgr.records = [_Rec("a.jpg", "ok")]
    w._recommit_if_library_changed()
    assert sched.calls == 1


def test_scheduler_errors_do_not_kill_the_watcher():
    class _Boom(_Scheduler):
        def library_changed(self):
            raise RuntimeError("boom")

    mgr = _Manager([_Rec("a.jpg", "pending")])
    w = _watcher(_State(mgr, _Boom()))
    w._recommit_if_library_changed()
    mgr.records = [_Rec("a.jpg", "ok")]
    w._recommit_if_library_changed()   # must not raise
