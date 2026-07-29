"""serve_log: durable serve-decision log — enable gate, append, size-bounded rotation."""

from __future__ import annotations

import json

from hokku_server import serve_log


def _entry(ts: str) -> dict:
    return {
        "ts": ts, "screen": "E1003", "ip": "1.1.1.1",
        "committed_before": "a.jpg", "pin": None, "chosen": "a.jpg", "served": "a.jpg",
        "drawer_matched_serve": True,
        "fleet_before": {"E1003": "a.jpg", "LR": "b.jpg"},
        "fleet_after": {"E1003": "c.jpg", "LR": "b.jpg"},
    }


def test_record_appends_json_lines_when_enabled(tmp_path):
    serve_log.configure(tmp_path, enabled=True)
    assert serve_log.is_enabled() is True
    for i in range(3):
        serve_log.record(_entry(str(i)))
    lines = (tmp_path / "serve_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(x)["ts"] for x in lines] == ["0", "1", "2"]


def test_disabled_is_noop_and_writes_no_file(tmp_path):
    serve_log.configure(tmp_path, enabled=False)   # the default
    assert serve_log.is_enabled() is False
    for i in range(5):
        serve_log.record(_entry(str(i)))
    assert not (tmp_path / "serve_decisions.jsonl").exists(), "disabled log must write nothing"


def test_configure_default_is_disabled(tmp_path):
    serve_log.configure(tmp_path)   # no enabled= → off
    assert serve_log.is_enabled() is False


def test_toggle_on_then_off(tmp_path):
    serve_log.configure(tmp_path, enabled=True)
    serve_log.record(_entry("on1"))
    serve_log.configure(tmp_path, enabled=False)   # e.g. Settings toggle flipped off
    serve_log.record(_entry("off"))
    serve_log.configure(tmp_path, enabled=True)    # back on
    serve_log.record(_entry("on2"))
    lines = (tmp_path / "serve_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ts = [json.loads(x)["ts"] for x in lines]
    assert ts == ["on1", "on2"], "the 'off' entry must not have been written"


def test_record_before_configure_is_noop():
    serve_log._path = None
    serve_log._enabled = False
    serve_log.record(_entry("x"))  # must not raise


def test_rotation_bounds_file_and_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_log, "_MAX_BYTES", 2000)  # tiny cap to trigger rotation fast
    serve_log.configure(tmp_path, enabled=True)
    for i in range(60):
        serve_log.record(_entry(str(i)))
    live = tmp_path / "serve_decisions.jsonl"
    backup = tmp_path / "serve_decisions.jsonl.1"
    assert live.exists()
    assert live.stat().st_size < 2000, "live file must stay under the cap"
    assert backup.exists(), "one previous generation is kept"
    # only ONE backup generation — total on disk bounded to ~2x cap
    assert not (tmp_path / "serve_decisions.jsonl.2").exists()
    # newest entry survives in the live file
    last = json.loads(live.read_text(encoding="utf-8").splitlines()[-1])
    assert last["ts"] == "59"
