# Photo Edit Mode — Web Interface (Mobile · Tablet · Desktop)

> **Purpose.** A self-contained plan for building hokku's per-image **photo editing mode** (crop +
> dithering-pipeline tuning) into the new web UI (which is being built separately). It is **one editor
> core with two shells** — a **touch shell** (mobile / tablet) and a **desktop modal** — that share the
> exact same engine, controls, presets, crop math, and backend contract; only the chrome differs. A fresh
> implementer should be able to build both from this document plus the repo.
>
> **Visual sources of truth — two interactive prototypes** (both vanilla JS/Canvas; reimplement in the new
> web UI's framework). Treat them as the reference for *feel*; this doc is the reference for *behavior +
> the backend contract*.
>
> | Shell | Prototype | What it settles |
> |---|---|---|
> | **Touch (mobile / tablet)** | <https://claude.ai/code/artifact/85ff3b69-deef-4feb-83e7-b71d95514770> | Full-screen editor: control-free default, **bottom toolbar** navigator, **slide-up overlay bin**, crop gestures, sliders, photo mat. |
> | **Desktop modal** | <https://claude.ai/code/artifact/f9135249-d913-4698-abd1-a965741100ea> | Pop-up **modal over the gallery**: same editor with a persistent **right tool rail** + a **controls panel** that expands beside it; mouse crop (drag / wheel-zoom / handles / dbl-click reset). |
>
> **Both prototypes run the same editor engine** — identical dither kernels, tonal chain, `DITHER_SCHEMA`
> (the four tool groups + bipolar ranges), presets, and crop math (`resizeTo`/`snapBack`), plus the same
> photo mat. What differs is only (a) the **shell** (§3.1–3.7 = shared + touch shell; §3.8 = desktop shell)
> and (b) the **preview render resolution** in `renderTo` (tuned to each canvas size — a preview knob, not
> a logic change). So build the editor **once** as a shared component and mount it in whichever shell the
> viewport calls for. (A naive whole-`<script>` diff will show the shell wiring differences; the engine
> *functions* match.)

---

## 1. What this is, and what it is not

Today hokku has one way to get a photo onto a frame: **batch upload** → a background worker
auto-classifies (B&W / face) and dithers with a global pipeline. The only per-image control is a
button-triggered preview buried in the Config tab that edits the *global* pipeline.

This adds a **per-image editor**: upload one photo (or re-open an existing one), **crop/rotate** it to the
frame's 4:3 / 3:4 aspect, then tune the **dithering pipeline** against a live preview, and **Submit** to
have the server render it with those per-image settings. The same editor ships in **two shells** — a
full-screen **touch** view for mobile/tablet and a **desktop modal** that pops up over the gallery — served
by one and the same backend.

**Not in scope here:** the batch-upload flow (unchanged), and a separate simpler "brightness/contrast only"
casual page (may come later; this editor exposes the real pipeline).

---

## 2. Architecture decision (locked)

- **The server is the single authoritative renderer** for *every* image — batch and single-image edit —
  from the **settings the editor submits** (a per-image `ImageConfig` + a crop/rotation spec), **never**
  from client-produced pixels. One renderer = perfect parity (edited and batch images look identical on
  the frame) and **no dependence on host platform** (desktop, NAS, cloud, small SBC — all fine).
- **The browser preview is a preview only** — a fast, *visually faithful* (not bit-exact) client-side
  render so the user can make decisions without round-tripping the server on every slider tick. The truth
  appears when the server re-renders on Submit.
- **Review → re-adjust → reprocess is a core loop.** After conversion the user reviews the result, tweaks
  the dither settings, and resubmits; the server re-renders. Editing a fresh draft and re-editing an
  existing image are the **same mechanism**.
- **Platform-agnostic.** Nothing here assumes a particular host. Server-side render tuning (warm caches,
  singletons) helps weak hosts and is harmless on strong ones, but the design does not depend on it.

Why the browser is *not* the sole renderer: it would need pixel-identical parity with the server
(OpenCV CLAHE, error-diffusion float determinism, YuNet) **and** batch would have to move client-side too.
Not worth it — see §8.

---

## 3. UX specification (from the prototypes)

**§3.1–3.7 describe the shared behavior as embodied by the touch shell** (the mobile/tablet prototype).
**§3.8 describes the desktop-modal shell** — the *only* things that differ. Everything else (crop model,
the four tool groups + their controls, bipolar sliders, edit-dots, presets, photo mat, `•••` menu, submit
behavior, visual system) is identical across both.

### 3.1 Screen shell
- **Top bar:** `Cancel` · `EDIT` (uppercase, letter-spaced, centered) · `•••` · `Submit`. Fixed layout —
  nothing shifts between modes. **Submit** is fixed-width and swaps its label for a **checkmark icon** on
  tap (reverts after ~1.4 s) so `•••` never moves.
- **`•••` menu:** `Preset ▸` (the three presets), `↺ Auto (best-guess)`, `Reset all`.
- **Font:** Helvetica Neue (sans) throughout — top bar matches the controls.
- **Default view is control-free:** just the **photo** over the **bottom toolbar**. No sliders, no crop
  tools until a tool is tapped.

### 3.2 Bottom toolbar (the single navigator)
One row of icon+label buttons: **Crop · Tonal · Color · Palette · Kernel**. Each shows a **porcelain
edit-dot** when it holds non-default values; dots update **live** (including Crop, which lights when
zoomed/panned/rotated). Tapping a tool opens it; tapping the **active** tool (or the photo, or swiping the
bin down) returns to the control-free view.

### 3.3 Crop mode (iOS-Photos model)
- The photo **pans and pinch/wheel/slider-zooms** over a crop frame with **8 handles** (4 corners + 4 edge
  midpoints). **Dragging a corner pins the opposite corner; dragging an edge pins the opposite edge.** The
  frame stays **locked to the target aspect** (4:3 horizontal / 3:4 vertical). Outside the frame is dimmed;
  thirds grid overlaid.
- **On release, the frame smoothly lerps back to full/centered** while the image zooms so the framed region
  stays framed — i.e. the crop is re-expressed as image **zoom + pan**. **Pinch/zoom-out de-zooms and
  de-crops.** **Double-tap / double-click reverts the crop to defaults** (animated de-zoom + un-rotate).
- **Rotate ⟲ / ⟳** = 90° steps; **Horizontal / Vertical** reshapes the frame and the image's target
  orientation. Crop tools sit above the toolbar with a small gap; the zoom control is a minimal
  line+hollow-ring slider matching the panel sliders.

### 3.4 Dither adjustment (the four tool groups)
Tapping Tonal/Color/Palette/Kernel slides a **fixed-height overlay bin up over the lower preview**. It's an
**overlay** — the preview area never resizes, so **the photo never shifts** when controls appear or when
you switch groups. The bin shows ~**2.5 rows** at a time (scroll for the rest); short groups sit at the
bottom of the bin.

Controls per group (this is a **curated subset** of the server pipeline — see §5):
- **Tonal:** Autocontrast cutoff, Gamma, Midtone lift, Brightness, Contrast, Local contrast (CLAHE),
  Keepout feather. *(sliders)*
- **Color:** Global color enhance, Max enhance. *(sliders)*
- **Palette LUT:** LUT (9 options as a **pull-down menu**, opened as a sheet **anchored above the toolbar**
  so it stays in the editing space); conditional **Hue cutoff°** and **Neutral chroma** sliders appear for
  the relevant LUTs.
- **Kernel:** Algorithm (Floyd–Steinberg / Atkinson / Stucki, segmented); Serpentine scan (switch).

### 3.5 Sliders (Lightroom feel, bipolar)
- Minimalist: a thin track + a **hollow ring** knob (ring = dim grey; center filled with the background so
  the track doesn't show through). Label + value are **dim by default and brighten when off the default**.
- **Drag anywhere on the row** to scrub; **double-tap / double-click** to reset to default.
- **Bipolar:** the default sits **dead-center** of each slider — see the ranges table in §5. Robustness:
  clean up scrub state on `pointerup`/`pointercancel`/`lostpointercapture` (avoids a stuck highlight when
  capture is lost).

### 3.6 Photo mat (brightness preview)
Behind the dithered preview, render a larger **off-white 4:3 / 3:4 "mat"** (≈`#EBE8E0`), with the photo
inset ~8% — like a matted print — so the user judges brightness against the frame's white ink, not black.
Sharp corners (no rounding on the image/mat). The preview centers roughly where the crop image centers.

### 3.7 Visual system (share with the new web UI)
Porcelain accent `#EDEBE5` (ink `#14140F`) on a cool near-black (`#0B0C10` / `#17171C`), text `#F3F2EF`,
dim `#8C8B87`; the six measured **Spectra inks** are the palette (`display.py PALETTE_MEASURED_RGB`).
Touch targets ≥44 px. **Both shells are the same dark editor surface** regardless of the surrounding page
theme (the modal keeps its dark identity even on a light page).

### 3.8 Desktop-modal shell (what differs from the touch shell)
Same editor, re-laid-out for a wide, mouse-driven window. Everything in §3.2–3.6 is preserved; only the
chrome changes:

- **Container = a modal, not a full screen.** A centered dark card (`min(1120px,96vw) × min(740px,92vh)`,
  18 px radius) over a **dimmed + blurred backdrop**; opens over the gallery ("Upload & edit", or click an
  existing image). **Esc** and **backdrop-click** close it; `role="dialog"` / `aria-modal`. The modal is
  self-contained and dark on any page theme.
- **Body = three columns:** a large **preview stage** (left, flexes), a **controls panel** (middle,
  collapsible), and a persistent **tool rail** (right, ~82 px).
- **Tool rail replaces the bottom toolbar** — the same navigator (**Crop · Tonal · Color · Palette ·
  Kernel**), rendered as a vertical strip of icon+label buttons with the same live **edit-dots**. This is
  the desktop-idiomatic "one navigator at rest".
- **Controls panel replaces the slide-up bin.** Selecting a tool **expands the panel** beside the rail
  (its *width* animates 0 → 312 px, the mirror of the touch shell's height animation); clicking the active
  tool, or the preview, collapses it back to the **control-free** default. Because desktop has vertical
  room, a group shows **all** its rows at once (no 2.5-row cap). Crop tools live in this panel (aspect
  segmented, rotate ⟲/⟳, zoom slider) when Crop is active; each group also gets a **Reset group** action.
- **Crop is mouse-driven:** drag inside the frame to reposition, drag a handle to reshape (snaps back on
  release), **scroll-wheel** to zoom, **double-click** to reset. (The touch prototype's pinch still works
  on a touch laptop — the pointer handlers are shared.)
- **No bottom-bar-anchored sheets:** the LUT pull-down opens as a small dropdown **anchored below its
  button** within the modal (clamped to the modal bounds), not a bottom sheet.
- **Not present on desktop:** the header six-ink strip (removed — read as noise on the wide header) and the
  swipe-down grabber (there's no bin to swipe).

**Implementation gotcha carried from the prototypes:** panels are toggled via the `hidden` attribute, but
an author `display` (e.g. `.crop-panel{display:flex}`) **overrides the UA `[hidden]{display:none}` rule** —
so include a `[hidden]{display:none!important}` reset (or toggle a class), or the crop tools leak into the
dither panels. This bit the desktop prototype once; keep the reset.

---

## 4. Data model / backend contract

The editor's job is to produce, per image, a **per-image `ImageConfig`** (the dither settings) plus a
**crop/rotation spec**, hand them to the server, and let the server render.

- **`ImageConfig`** already exists ([webserver/hokku_server/image_config.py](webserver/hokku_server/image_config.py))
  — the editor edits a subset of its fields (§5) and leaves the rest at their preset defaults.
- **Crop/rotation:** the editor works internally in image zoom/pan/rotation, but must persist a
  **normalized crop `rect {x,y,w,h}` in rotated-source space + `rotation_quarters` (0–3) + `target`
  orientation** (landscape/portrait). Derive the rect at submit from the current zoom/pan (the source
  region under the full frame). **This mapping is the one genuinely new algorithm to get right** — verify
  it round-trips: rect → server render fills the frame at the target aspect with no letterbox.

### 4.1 Backend changes (repo-relative references)

- **Render path — manual crop + rotation.** `_prepare_canvas`
  ([image_abc.py:212](webserver/hokku_server/image_abc.py#L212)) only does auto fit/cover today. Add a
  pre-transform: rotate by `rotation_quarters·90°`, then crop to the normalized rect, before scaling.
  Thread the crop/rotation through `render_indices` / `render_panel_bytes` / `render_preview_png` and
  `render_worker.render_one`, the same way `crop_to_fill_threshold` is threaded. Add
  `rotation_quarters` + `crop_rect` to **`ScreenImageConfig`**
  ([screen_image_config.py:14](webserver/hokku_server/screen_image_config.py#L14)) and its `cache_slug()`
  so crop changes invalidate the cache.
- **Per-image persistence.** Add to **`ImageRecord`**
  ([image_record.py:17](webserver/hokku_server/image_record.py#L17)) two optional JSON fields:
  `edit_image_config: dict | None` (a frozen `ImageConfig` blob; `None` = use classifier decision) and
  `edit_crop: dict | None` (`{rotation_quarters, rect{x,y,w,h}, target}`). Extend `from_dict`; bump
  `_DB_VERSION` ([image_manager_abstract.py:47](webserver/hokku_server/image_manager_abstract.py#L47))
  (nuke-and-re-render migration is fine — artifacts are re-derivable). Add an **`effective_orientation`**
  property (`edit_crop.target` when set, else `native_orientation`) used by the scheduler pool + preview.
- **Effective-decision helper (correctness seam).** Add `_effective_decision(name, rec)` on the manager:
  base = `classifier.decision_for(...)`; if `edit_image_config` is set, `replace(base, image_config=…)`.
  **Wire it symmetrically** into (1) `_submit_one` dispatch, (2) `sync()` phase-2 decisions, and
  (3) the **reconcile OK-recheck** ([:702-717](webserver/hokku_server/image_manager_abstract.py#L702)) —
  else an edited OK image **re-pends forever** (the #1 correctness trap; test it). Crop/rotation flow into
  the `ScreenImageConfig` at dispatch; render only the `effective_orientation` for cropped images.
- **Draft lifecycle.** Add `ConvertStatus.DRAFT`. `manager.add_draft(name, bytes)` registers a record as
  DRAFT and writes the original (so previews can resolve it) but keeps it **inert** — `sync()`'s pending
  filter, the scheduler pool, and `show_next` all ignore non-OK/non-pending, so a draft never
  auto-converts or joins rotation until Submit. Editor Cancel/`beforeunload` → `DELETE /image/<name>`;
  plus a startup sweep of stale DRAFTs.
- **Endpoints** (in `create_app`, near [flask_app.py:340](webserver/hokku_server/flask_app.py#L340)):
  - `POST /hokku/api/upload_draft` — single file → `add_draft` → `{name}`.
  - `GET /hokku/api/image/<name>/suggested_config` — wraps `ImageClassifier.decision_for` →
    `{image_config, crop_to_fill_threshold, face_bboxes, is_bw, orientation, source_w, source_h}` (the
    editor's initial state + one-shot detection results). Release the face detector after.
  - `POST /hokku/api/image/<name>/edit` — body `{image, edit_crop}` → validate via
    `_image_config_from_dict` → `commit_edit(...)` stores both fields, flips DRAFT→PENDING **or**
    OK→PENDING+clear slugs (the **reprocess** path, like `retry`), saves DB. Background `sync()` renders.
    **No inline render in the request.**
  - **Extend** `POST /hokku/api/dither/preview` to accept `max_side_px`, explicit `orientation`, and
    `crop`/`rotation` (also fixes the portrait-draft bug: `native_orientation` asserts OK — add
    `orientation_from_dims(w,h)` and pass an explicit orientation). Keep this endpoint as an **optional
    parity/fallback** — it is **not** on the interactive path (see §7).

### 4.2 Portrait-draft preview bug (fix while here)
`native_orientation` asserts `convert_status==OK`
([image_record.py:48](webserver/hokku_server/image_record.py#L48)); the preview endpoint currently dodges
by forcing LANDSCAPE, so portrait drafts preview sideways. Add `orientation_from_dims(w,h)` (no assert) and
have the editor pass an explicit orientation from `suggested_config`.

---

## 5. Dither controls — fields, curation, and bipolar ranges

The editor exposes a **curated subset** of `renderDitherPanel`
([webserver/templates/index.html:1631-1708](webserver/templates/index.html#L1631)). Dropped controls keep
their preset defaults in the committed `ImageConfig` (the full raw panel still lives on the Config tab):
- **Dropped from editor:** Sharpening amount/radius, Pre-dither noise (Tonal); the Adaptive-saturation
  space selector + Low/High chroma thresholds (Color); the **entire Dynamic-range-compression group**
  (`drc_*`, `scale_chroma`, `adaptive_vivid`, `vivid_*`) — all left at preset values.

**Bipolar sliders — center the default.** Each numeric slider presents a **display band symmetric around
its default** so the default sits dead-center and the user pushes ±. The band is a *display* range only;
clamp committed values to the field's real `ImageConfig` limit. Bands (all strictly inside the real limits,
centers on-grid with the step):

| Field | Default (center) | UI band | step | Real limit |
|---|---|---|---|---|
| `prepare_autocontrast_cutoff` | 0.5 | 0 – 1.0 | 0.05 | 0–49 |
| `prepare_gamma` | 0.88 | 0.48 – 1.28 | 0.05 | 0.1–3 |
| `prepare_midtone` | 1.02 | 0.62 – 1.42 | 0.05 | 0.5–2 |
| `prepare_brightness` | 1.0 | 0.5 – 1.5 | 0.05 | 0.1–3 |
| `prepare_contrast` | 1.1 | 0.6 – 1.6 | 0.05 | 0.1–3 |
| `clahe_clip_limit` | 1.75 | 0 – 3.5 | 0.05 | 0–5 |
| `clahe_keepout_feather` | 0.015 | 0 – 0.03 | 0.001 | 0–0.05 |
| `color_enhance` | 1.25 | 0.5 – 2.0 | 0.05 | 0.5–3 |
| `saturate_max_enhance` | 1.25 | 0.5 – 2.0 | 0.05 | 0.5–3 |
| `dither.hue_cutoff_deg` | 95 | 10 – 180 | 1 | 10–180 |
| `dither.neutral_chroma` | 8 | 0 – 16 | 0.5 | 0–50 |

Widen any single band later if a control wants headroom — keep it symmetric (`default ± Δ`). Reset target =
the **default-preset** value (the center); a control the active preset moved off-center reads as "changed".

**Presets (must match `presets.py` exactly):** `_hue_aware("floyd_steinberg", serpentine=True)` is the
default; `atkinson_hue_aware` = `_hue_aware("atkinson")` (serpentine off); `floyd_steinberg_bw` = `_bw(...)`
= lut `bw`, `color_enhance=1.05`, `adaptive_saturate_space="off"`, `adaptive_vivid=False` (leaves
`saturate_max_enhance` at 1.25). Labels: "Floyd-Steinberg (hue-aware)", "Atkinson (hue-aware)",
"Floyd-Steinberg (neutral)". **Auto/best-guess** = the default preset for a normal color photo (the
server's classifier swaps to the B&W/face preset only when it detects those).

---

## 6. Client-side preview (browser render, fidelity-only)

Render the live preview in the browser so the server does **no** interactive work:
- Ship the server's **precomputed 32³ RGB→palette-index LUT** (`lut_and_scale_for_dither_config` output,
  ~32 KB) for the chosen config → **no CIELAB/OKLAB/CAM16 math in the browser**, just a grid lookup. This
  is the key trick that makes even CAM16-UCS trivial client-side.
- Port the **measured 6-ink palette**, the tonal chain (gamma/midtone/brightness/contrast; an *approximate*
  CLAHE is fine — see §8), color enhance, and the **FS/Atkinson/Stucki** kernels (serpentine) to JS/WebGL,
  run at reduced resolution on a Worker/WebGL. The prototype demonstrates the shape (real FS dither to the
  six inks in-browser).
- **Detection is server-side and one-shot:** the `suggested_config` endpoint returns `is_bw` + face bboxes;
  the editor uses them (e.g. for keepout visualization). No need to run YuNet in the browser.
- The preview is faithful, not exact — the server render on Submit is the truth. Keep
  `POST /dither/preview` only as an optional "match server exactly" / fallback path, off the interactive
  loop.

---

## 7. Replication feasibility (why fidelity-only is the right call)

From a repo read of the pipeline:
- **Exact & easy in-browser:** the 32³ LUTs (ship them), and the hand-rolled CIELAB/OKLAB/DRC/kernel math
  (direct port).
- **Hard to bit-match by hand:** OpenCV **CLAHE** / `cvtColor` / `GaussianBlur`, PIL
  autocontrast/enhance/unsharp, and the **chaotic error-diffusion float determinism** (a 1-LSB diff can
  flip a pixel and propagate). True bit-exact would require running the *same compiled code* (opencv.js for
  the cv2 parts incl. `FaceDetectorYN`, plus a float32-matched WASM/Pyodide port).
- Because the server re-renders authoritatively on Submit, the preview **needn't** be bit-exact — so we
  skip all of the above hard parity work. This is why option (a) (server render) was chosen.

---

## 8. Phasing

- **Phase 0 — render seams.** Manual crop+rotation in `_prepare_canvas` + `ScreenImageConfig`; the
  **crop-rect ↔ zoom/pan mapping** (§4); `orientation_from_dims` fix; extend `/dither/preview` with
  `max_side_px`/`orientation`/`crop`. Independently testable before any UI.
- **Phase 1 — persistence spine.** DRAFT status; `edit_image_config`/`edit_crop` + `_DB_VERSION` bump;
  `_effective_decision` wired into dispatch/sync/**reconcile**; `add_draft`/`commit_edit`; the
  `upload_draft` / `suggested_config` / `edit` endpoints. Full test coverage via `test_client`.
- **Phase 2 — the editor UI** in the new web UI. Build the **editor core once** (state + crop math + the
  four dither groups + bipolar sliders + LUT pull-down + switches + photo mat + client-side preview + the
  `••• → Preset/Auto/Reset` menu + checkmark Submit), then mount it in **two shells**: the **touch shell**
  (bottom toolbar + slide-up overlay bin — §3.1–3.7) and the **desktop modal** (right tool rail + expanding
  controls panel — §3.8). Pick the shell by viewport/pointer. Both entry points work in both shells:
  **Upload & edit** (draft) and **review/re-edit** (existing OK image → reprocess). Both prototypes are the
  reference implementations — reuse their engine block verbatim.
- **Phase 3 — gallery/review polish.** Original↔Dithered review, and a small dithered-thumbnail path if the
  gallery needs it.

---

## 9. Highest-risk items

1. **Reconcile re-pend loop** — `_effective_decision` (incl. crop/rotation) must be used symmetrically in
   dispatch **and** reconcile, or edited images re-convert forever. Test explicitly.
2. **Draft never auto-converts** — the whole "edit before commit" property rests on DRAFT being invisible
   to the pending filter + reconcile auto-register. Verify no path flips DRAFT→PENDING implicitly.
3. **Crop-rect ↔ zoom/pan mapping** — the normalized rect through rotation + scale must land pixel-correct
   and route to the right orientation.
4. **Preview fidelity vs. the server render** — the client preview should be close enough that the
   post-Submit result rarely surprises; it does not need to be exact.

---

## 10. Verification

- **Unit/API** (`webserver/tests/test_flask_api.py`; fixtures `bare_client`/`synced_client`,
  `_upload_bytes`): `suggested_config` returns a full round-trippable `image_config` + `orientation`;
  preview with `max_side_px`/`orientation`/`crop` on a **draft** returns a correctly-shaped PNG (proves the
  render seams + portrait fix); `commit_edit` with a distinctive value + crop → `sync()` → the resulting
  slug equals `ScreenImageConfig(edited_cfg, target, crop_rect, rotation).cache_slug()` (proves
  `_effective_decision` is used); a second `sync()` leaves it **OK** (no re-pend); `ImageRecord`
  `to_dict`/`from_dict` round-trips with/without the new fields; `add_draft` stays DRAFT through a full
  `sync()` with no artifacts and absent from the scheduler; DELETE removes a draft.
- **End-to-end (manual):** run the server, open the mobile editor, Upload & edit a landscape and a portrait
  photo: crop with each handle + confirm the snap-back, rotate both ways, tune each group (confirm bipolar
  centering + live edit-dots + double-tap reset), Submit, confirm the server renders and the frame serves
  it; then **re-open the converted image, re-adjust, resubmit**, and confirm it re-renders. Check the
  client preview is a reasonable match to the server result.

---

## Appendix — key repo files
- `webserver/hokku_server/image_abc.py` — `_prepare_canvas`, `_apply_prepare_enhancements` (PIL+OpenCV).
- `webserver/hokku_server/image_renderer.py` — `render_indices`, `compress_dynamic_range` (DRC).
- `webserver/hokku_server/dither_streaming.py` — color spaces, LUT builders, diffusion; the 32³ LUTs.
- `webserver/hokku_server/display.py` — `PALETTE_MEASURED_RGB`, `indices_to_panel_bytes`.
- `webserver/hokku_server/image_config.py` / `screen_image_config.py` / `image_record.py` — config + cache
  keys + record.
- `webserver/hokku_server/image_classifier.py` / `face_detect_yunet_opencv.py` — B&W + YuNet detection.
- `webserver/hokku_server/image_manager_abstract.py` — sync/dispatch/reconcile; `_DB_VERSION`.
- `webserver/hokku_server/flask_app.py` — endpoints; `POST /hokku/api/dither/preview`.
- `webserver/hokku_server/presets.py` — the three presets (source of truth for defaults).
- `webserver/templates/index.html` — `renderDitherPanel` (the full control set + real ranges).

**Design prototypes (interactive, self-contained — the visual/interaction reference for each shell).**
The source HTML lives **in this repo** next to this plan, and each is also published as a live artifact:

| Shell | Repo file (read this) | Live artifact |
|---|---|---|
| Touch (mobile / tablet) | [Plans/prototypes/touch-editor-mobile-tablet.html](prototypes/touch-editor-mobile-tablet.html) | <https://claude.ai/code/artifact/85ff3b69-deef-4feb-83e7-b71d95514770> |
| Desktop modal | [Plans/prototypes/desktop-modal-editor.html](prototypes/desktop-modal-editor.html) | <https://claude.ai/code/artifact/f9135249-d913-4698-abd1-a965741100ea> |

Both prototypes share one engine (dither kernels, `DITHER_SCHEMA`, presets, tone chain, crop math); only
their shell markup/CSS + navigator wiring and the `renderTo` preview resolution differ. **Lift the engine
functions verbatim into the real component**, then build the two shells around it. The prototypes are
self-contained single HTML files (vanilla JS/Canvas, in-browser sample photos) — open them directly to
interact, and read their `<script>` for the reference implementation of each engine function.
