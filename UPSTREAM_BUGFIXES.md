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

## 2. "Letterbox fill limit" maxes at 100%, one hair below what a real 3:2 photo needs

**Where:** the fill decision in `image_abc.py` — `use_cover = crop_to_fill_threshold > 0.0 and
zoom_ratio <= crop_to_fill_threshold` — plus the settings slider that feeds it
(`<input type="range" id="crop-to-fill-threshold" min="0" max="100">` in `templates/index.html`).
Verified present in `upstream/main`: the slider bounds and the comparison are both unchanged.

**Applies to:** any screen configured **portrait** with landscape photos in the library (and the
mirror case: landscape screens with portrait photos). Not panel-specific — it is pure aspect
arithmetic, so it applies to every model.

### Symptom
A photo letterboxes with enormous bars no matter how far the "Letterbox fill limit" slider is
pushed — including all the way to its 100% maximum. It looks like the setting is broken or being
ignored. Sliding it to 100% changes nothing for these photos, so users assume a caching bug.

### Root cause
Filling a frame costs zoom equal to the aspect ratio gap. For a landscape photo on a **3:4
portrait** frame:

```
zoom_ratio needed = (4/3) x (photo width / photo height)
```

| Photo | Aspect | Zoom needed |
| --- | --- | --- |
| 4:3 | 1.3333 | 77.8% |
| **perfect 3:2** | **1.5000** | **100.00%** |
| Full-frame camera (7392x4896) | 1.5098 | 101.3% |
| 35 mm film scan (5728x3790) | 1.5113 | 101.5% |
| 16:9 | 1.7778 | 137.0% |

The slider's ceiling of 100% lands **exactly** on the perfect-3:2 boundary. But no real camera is
exactly 3:2 — sensors and scans come in at 1.500–1.512 — so essentially every "3:2" photo needs
slightly MORE than 100% and is unreachable. The ceiling is not a policy choice, it is an arbitrary
round number that happens to sit a fraction below the single most common photo shape.

Measured on a 297-image library: **18 photos could never fill a portrait frame at any setting.
Every one of them needed between 100.02% and 101.51%** — not one needed more than 105%. They were
denied by roughly one percentage point.

The strict `<=` also has no tolerance for whole-pixel rounding, which matters because the decode
step clamps the long side (`_MAX_SOURCE_LONG_SIDE`) and the clamped dimensions are floored to
integers. A source whose aspect matches the frame on paper can come out a hair off after that
rounding and be charged zoom it should not owe — at 0% that pads a sub-pixel gap into a crisp 1 px
white line on one edge; at the ceiling it letterboxes the photo whole.

### Fix
Two independent parts; the first is the bug, the second is a companion hardening.

1. **Raise the slider ceiling** past the aspect gaps that real photos actually present. 150% clears
   every landscape aspect through 16:9 (137%), which is the widest a photo can be and still fill a
   portrait frame with real pixels:
   ```html
   <input type="range" id="crop-to-fill-threshold" min="0" max="150" step="1" value="10">
   ```
   Anywhere the threshold is clamped server-side, lift the clamp to the same constant rather than
   leaving a silent `min(x, 1.0)` that truncates what the slider now sends. Worth naming the
   ceiling as one shared constant so the UI and the clamps cannot drift apart.

2. **Give the comparison a rounding tolerance** so integer dimensions cannot cost a full letterbox:
   ```python
   _ZOOM_SNAP_RATIO = 0.005   # 0.5%
   use_cover = zoom_ratio <= crop_to_fill_threshold + _ZOOM_SNAP_RATIO
   ```
   Note this also drops the `crop_to_fill_threshold > 0.0 and` guard, which is what makes the
   tolerance work at the 0% setting (the 1 px hairline case). If upstream prefers 0% to mean
   "never cover, ever", keep the guard and add the tolerance only above zero — the ceiling fix
   above is independent of this choice.

If the render cache key encodes a framing-policy version, bump it: the decision changes for photos
sitting near a boundary, and their already-cached renders are otherwise reused forever.

### Rationale / safety
- **150%, not "unlimited"**: the slider still means "the most I will crop", and a ceiling keeps it a
  limit rather than an on/off switch. 137% is the real-world worst case, so 150% has margin without
  becoming meaningless.
- **0.5% tolerance**: ~10x the worst rounding error (1 px on a ~2100 px edge) and far below a
  visible zoom step (6 px on a 1200 px panel), yet nowhere near a genuine aspect gap — the smallest
  real gap in play is 4:3-on-3:2 at 12.5%, twenty-five times larger. It cannot silently swallow a
  crop the user did not ask for.
- **No change for anyone happy today**: existing configs keep their stored value, and every photo
  already reachable under 100% behaves identically. The change only makes previously impossible
  settings expressible.
- Users who want bars gone unconditionally are still better served by a separate explicit control;
  raising the ceiling fixes the arithmetic, it does not try to be that feature.

### Suggested tests
- A real 3:2 sensor size (7392x4896) on a portrait frame: letterboxes at threshold 1.00, fills at
  1.05. This is the regression.
- 16:9 (3840x2160) on a portrait frame: fills at the new ceiling, letterboxes at 1.30 — proves the
  ceiling was chosen to reach the real worst case.
- A source whose aspect matches the frame to within a pixel fills at threshold 0.0 (no hairline).
- A genuinely mismatched source (2% gap) still letterboxes at threshold 0.0 — the tolerance is
  rounding-sized, not policy-sized.
- If a mono/secondary pipeline shares the decision, assert the raised threshold survives its own
  clamp, not just the primary renderer's.

---

## 3. A power cut can silently erase the image database (and every per-image edit)

**Where:** `atomic_write_json` in `filesystem.py`, and the load path of every JSON state store
(`image_manager.py` / `serve_scheduler.py` / `image_classifier.py`). Verified byte-identical in
`upstream/main`: the write has no `fsync`, and `_load_db` still does
`except (json.JSONDecodeError, OSError): logger.warning("... (starting empty)"); return`.

**Applies to:** every install, any unclean shutdown — power loss, hard reset, host crash. Not
panel- or platform-specific, though NTFS makes it easy to hit.

### Symptom
After an unclean shutdown the server starts, logs

```
WARNING Failed to load image_manager.json: Expecting value: line 1 column 1 (char 0) (starting empty)
INFO    Registered new image: ...   (x every photo in the library)
```

and comes back with the library intact but **every per-image edit gone** — crops, per-image
image configs, any per-image overrides — plus the real `added_at` history, so "newest first"
silently becomes filesystem scan order. We lost 129 editor crops this way on 2026-09-06.

### Root cause — two independent defects that combine
1. **The "atomic" write is not durable.** `atomic_write_json` does
   `open → json.dump → os.replace`. `os.replace` is atomic for the *rename*, but nothing
   flushes the data first. NTFS journals metadata (the rename) and not file data, so the
   directory entry can reach the platter while the contents are still in the page cache. A
   power cut then leaves a file that **exists and is empty** — hence the byte-zero parse error.
2. **A failed load becomes an authoritative empty state.** `_load_db` catches the error, logs,
   and returns with zero records. The next `_save_db()` — and registration saves the whole file
   *per photo*, so hundreds follow immediately — writes that emptiness over the file. **The
   crash corrupts; the application deletes.** Until that first save the old blocks are still on
   disk and recoverable.

Note `AppConfig.load` already gets this right: on a parse failure it `sys.exit(1)` rather than
overwriting the user's settings with defaults. The correct pattern is already in the tree; the
data stores just don't use it.

### Fix
Three parts; (1) alone prevents most occurrences, (2) makes the rest survivable, (3) is what
stops a corruption becoming a deletion.

1. **fsync before the rename:**
   ```python
   with open(tmp, "w") as f:
       json.dump(payload, f, indent=2)
       f.flush()
       os.fsync(f.fileno())
   os.replace(tmp, path)
   ```
   Measured cost on a 235 KB DB: **7.0 ms → 7.6 ms per save** (+0.6 ms). Over a 358-photo
   rescan that is 2.5 s → 2.7 s. There is no performance argument against it.
2. **Keep one previous generation** — copy the current file to `<name>.bak` before replacing.
   Note this is worthless on its own: on the failed-load path the rebuild's first save rotates
   the corrupt file into `.bak` and the next pushes the good copy out. It only helps combined
   with (3).
3. **A failed load must never overwrite.** Quarantine the unreadable file as
   `<name>.corrupt-<timestamp>` (keep the evidence), try `.bak`, and then branch on how
   replaceable the store is:
   - **image DB** (holds irreplaceable user edits): refuse to start, exactly as config does.
     Frames keep showing their last image; nothing is destroyed; the operator fixes it
     deliberately.
   - **scheduler / classifier** (derived, re-computable): start empty, but only after the
     quarantine + `.bak` attempt.

Optional and independent: registration saving the entire DB per photo widens the crash window
enormously (358 full writes in 19 s during a rescan). Batching it per sync shrinks the exposure
and speeds up large uploads.

### Rationale / safety
- **fsync ordering is the whole point** — fsync *then* rename. Reversed, it protects nothing.
- **Refusing beats guessing** for the image DB: an offline server is recoverable in minutes; an
  overwritten database is not recoverable at all. Frames hold their last image, so the wall
  looks normal while it's down.
- **Quarantine rather than delete** — the corrupt file is the only forensic evidence, and
  keeping it also stops the next start tripping over the same file.
- **Only snapshot a file that just parsed** if you add dated backups; archiving in-memory state
  risks writing a corrupt copy over a good one.

### Suggested tests
- Assert the call ORDER: monkeypatch `os.fsync` and `os.replace`, write, assert `fsync`
  precedes `replace`. Ordering is the fix.
- Truncate the store to zero bytes, reconstruct the manager, assert it loads from `.bak` and
  that records are non-empty.
- Truncate with no `.bak`: assert it raises rather than returning empty, and that the corrupt
  file was moved aside rather than overwritten.
- End-to-end regression: save an edit/crop, zero the file, restart, assert the edit survives —
  and then run a sync (the step that used to make the loss permanent) and assert it still does.

---

<!-- Add further upstream-affecting fixes below as they are found, same format:
     Where (concept, not our path) / Applies to / Symptom / Root cause / Fix / Rationale / Tests. -->
