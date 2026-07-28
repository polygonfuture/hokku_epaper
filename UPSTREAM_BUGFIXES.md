# Upstream bug fixes — for defl/hokku_epaper `main`

> Bugs we found and fixed in our fork that ALSO exist in the upstream (`defl/hokku_epaper`)
> codebase, written so they can be re-implemented directly in upstream's own tree by a person or
> an LLM. Each entry is tree-agnostic: it names the CONCEPT (function/behavior) rather than our
> fork's file paths, because upstream's layout differs (V4 moved `webserver/hokku_server/` →
> `python/hokku/webserver/`). Verified against `upstream/main` (4.0.0 alpha 1) at the time of
> writing. Nothing here is a patch to apply — it's a description to re-implement.

---

## 1. Double-refresh at scheduled refresh times (frame refreshes the same slot twice)

**Where:** `calculate_sleep_seconds()` in `time_utils.py` (upstream:
`python/hokku/webserver/time_utils.py`). Confirmed present and byte-identical in upstream V4
except for the module import path — upstream did not change the logic.

**Applies to:** every screen model on the **scheduled-time** refresh mode
(`refresh_image_at_time`, default `("0600","1200","1800")`) — i.e. the default, out-of-the-box
configuration. Not specific to any panel.

### Symptom
At a scheduled refresh time (e.g. noon), a frame refreshes, then refreshes AGAIN ~30 s–a few
minutes later — two e-ink repaints and two rotation steps for one scheduled slot. Observed on
real hardware: both frames refreshed at 12:00, then did a second refresh of a different image
shortly after.

### Root cause
Frames wake on an RTC timer whose drift (~1–2% over a multi-hour sleep = minutes) means a frame
routinely wakes a little BEFORE a scheduled slot. The sleep calculation picks the first slot with
`candidate > now` and floors the result:
```python
for h, m in wake_times:
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate > now:
        return max(60, int((candidate - now).total_seconds()))
```
So a frame waking at 11:57 (3 min before the noon slot) is told to sleep `max(60, ~180)` = 180 s,
then wakes at ~12:00 and serves the noon slot — but it had ALREADY served (this wake did the
refresh). The `max(60, …)` floor turns "you're basically at the slot" into "nap and come back,"
producing a second refresh of the same slot. There is no server-side idempotency (no
"already served this slot" memory; every 200 response is a fresh, rotation-advanced image), so the
second wake repaints a different photo — visibly a double refresh.

### Fix
Treat a slot within a grace window of `now` (either side — imminent OR just passed) as the slot
THIS wake is serving, and sleep to the FIRST slot more than the grace ahead:
```python
_SLOT_GRACE_SECONDS = 300  # 5 min: comfortably > expected RTC drift, far < the gap between slots

# build today's slots + tomorrow's first slot as datetimes, then:
for candidate in candidates:                    # in chronological order
    secs = (candidate - now).total_seconds()
    if secs > _SLOT_GRACE_SECONDS:
        return int(secs)
```
A frame waking any time within ±5 min of noon now sleeps ~6 h to 18:00 instead of napping back to
noon. The `max(60, …)` floor is no longer needed (the grace guarantees the returned value is
always > 300 s in the scheduled path).

### Rationale / safety
- **Grace = 5 min**: must be larger than realistic RTC drift over a sleep (minutes), and far
  smaller than the interval between slots (default 6 h) so it can never swallow a genuinely
  distinct slot. 5 min satisfies both with wide margin.
- **Both-sides window**: covers waking slightly early (the main case) AND slightly late (a frame
  that woke a bit after the slot must not be told to sleep ~6h-minus-epsilon and then, on a small
  drift, re-serve — the grace makes "just after" also target the next slot cleanly).
- **No behavior change for normal wakes**: a wake well inside a gap (e.g. 09:00) still targets the
  next slot (12:00) exactly as before — verified.
- `debug_fast_refresh` mode and the "no times configured → 6 h" default are untouched.

### Suggested tests (upstream has `test_time_utils.py` to extend)
- Wake 30 s before a slot → sleeps to the NEXT slot (~6 h), not ~30–60 s.
- Wake a few minutes before a slot → same.
- Wake exactly on / a couple minutes after a slot → targets the next slot.
- Mid-gap wake (e.g. 09:00) → unchanged (~3 h to noon). Guards against the grace perturbing
  non-boundary cases.

---

<!-- Add further upstream-affecting fixes below as they are found, same format:
     Where (concept, not our path) / Applies to / Symptom / Root cause / Fix / Rationale / Tests. -->
