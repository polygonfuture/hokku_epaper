# Plan: Editor crop rotation follows image rotation (Bug #6)

> New plan 2026-07-27. Extracts Bug #6 from EDITOR_NOCROP_PLAN.md into its own focused,
> shippable fix (user chose NARROW scope — just the rotate-aspect lock, not the #3/#4 redesign).
> Client-only (`editor.js`). No server/pipeline/test-suite impact.

## Context

In the photo editor, pressing **Rotate 90°** spins the image but leaves the crop box at its old
aspect — a Horizontal (4:3) crop stays horizontal over a now-portrait-shaped rotated image,
silently producing a **partial, mismatched crop** that reframes the photo. Desired: the crop
rotates WITH the image so it keeps framing the same content — a 90° rotate auto-flips the crop
aspect (H↔V) to stay locked to the content.

**Decisions (user-confirmed, CORRECTED 2026-07-27):**
- **Aspect and rotation are PERMANENTLY COUPLED:** rotating the image ALWAYS flips the crop
  aspect (H↔V), with NO exceptions — even if the user just manually set the aspect. There is no
  "pin". (An earlier "manual choice sticks" reading was wrong and was reverted — see below.)
- **Narrow scope:** fix ONLY the rotate-aspect coupling (Bug #6). Leave the related editor-crop
  redesign (#3 no-crop state, #4 edit-preview phantom auto-crop, rotation-only unreachable) for a
  separate later rework — shared root cause but out of scope here.

> Correction history: first implemented with an `ED.aspectUserSet` flag so a manual aspect tap
> stopped rotation from auto-flipping. User clarified this was wrong: rotation must ALWAYS rotate
> the crop, even when aspect and image are "out of match". The flag was removed; `rotBy` now
> flips the aspect unconditionally. Simpler than the flag version.

## Root cause (verified by exploration)

All crop/rotate state is on the `ED` singleton in `webserver/static/app/js/editor.js`. The bug is
entirely in `rotBy(d)` (line 305):
```js
function rotBy(d) { forkMonoCropIfEditing(); ED.rotation = (ED.rotation + d) % 4; initCrop(); renderRail(); applyPanelState(); }
```
It mutates `ED.rotation` but NOT `ED.aspect`. `buildRotated()` (117) swaps rotatedSrc dims on odd
rotation, but `computeCropRect()` (128, `ar = ED.aspect==="H" ? 4/3 : 3/4`) still lays the old
box → `normCrop()` (111) yields a partial rect → Submit (1079,
`editCrop = {rotation_quarters: ED.rotation, rect: normCrop(), target: target()}`) sends it.

No existing flag distinguishes a user-tapped aspect from an auto-followed one (`ED.aspect` is
written only at `loadCrop` 71, `restoreCrop` 140, `setAspect` 304, `openEditor` init 1048).

## Approach (AS SHIPPED — minimal, editor.js only)

1. **In `rotBy(d)`, ALWAYS flip the aspect:** after bumping `ED.rotation`, unconditionally
   `ED.aspect = ED.aspect === "H" ? "V" : "H"` then `syncAspectUI()`, before `initCrop()`. Each
   `rotBy` is exactly ±1 quarter (rotL=3≡−1, rotR=1) = one 90° turn = one H↔V flip. 180° (two
   calls) flips back to original, correct. Existing `initCrop()`→`drawCrop()` +
   `renderRail()`/`applyPanelState()` tail redraws.

2. **No flag, no `setAspect` change, no init changes.** `setAspect` just sets the aspect as
   before; a later rotate still flips it. Rotation and crop aspect are permanently coupled.

3. **No `restoreCrop` change** — it serializes/replays the `(rotation_quarters, target, rect)`
   triple independently; the round-trip stays correct since Submit reads all three from the same
   post-flip `ED` state.

4. **Mono fork:** `snapCrop()`/`loadCrop()` (70–71) already carry `aspect`, and `rotBy` calls
   `forkMonoCropIfEditing()` first — a forked mono crop inherits the flipped aspect correctly.

## Critical files
- `webserver/static/app/js/editor.js` — ONLY file changed: `rotBy` (305) guarded flip +
  `syncAspectUI()`; `setAspect` (304) set flag true; `openEditor` (~1048) + `restoreCrop` (138)
  init flag false. Reuse existing `syncAspectUI()` (287), `initCrop()` (134), `computeCropRect()`
  (128) — no new rendering code.

## Verification (static JS, served live; render + LOOK per orientation-debugging-method)
1. **Auto-flip:** open a LANDSCAPE photo → Crop (aspect Horizontal) → Rotate right → EXPECT image
   rotates 90° AND aspect auto-flips to Vertical, box still framing the same content. Rotate again
   → back to Horizontal. 4 rotations return to start.
2. **Manual sticks:** open a photo → tap the aspect toggle (deliberately pick V on a landscape
   photo) → Rotate → EXPECT aspect does NOT auto-flip (stays V).
3. **Round-trip:** make a rotated crop → Submit → reopen editor → EXPECT the crop restores exactly.
   View Details Spectra 6 + Mono 16 match the intended framing.
4. **Optional Playwright:** drive `rotBy` on the real served editor; assert `ED.aspect` flips when
   `!aspectUserSet`, holds when set; screenshot the crop box.
5. `node --check editor.js`; confirm served live. Python suite (798 passing) unaffected (client-only).

## Out of scope (deferred to the editor-crop-model redesign — see EDITOR_NOCROP_PLAN.md)
- Bug #3 (no-crop state; Submit can't express "no crop").
- Bug #4 (Spectra edit-preview phantom panel-aspect auto-crop vs the real letterbox).
- Rotation-only-unreachable finding.
- Per-snapshot `aspectUserSet` memory for forked mono crops.
