# Hokku Web UI — Redesign notes & feature-gap review

Working notes for the dark, modern redesign of the Hokku web UI. The redesign is prototyped as a
self-contained artifact (off-black `#0B0C10` ground + porcelain `#EDEBE5` accent). This doc tracks
progress against the original plan and the feature gaps vs. the current in-repo UI
(`webserver/templates/index.html`).

_Last updated during design iteration. This is a design/planning doc, not shipped code._

---

## 1. Status vs. the original plan

The plan was **Step 1 (design artifacts)** → **Step 2 (implement the real static app at `webserver/static/app/`)**.

| Plan item | Status |
|---|---|
| 1a — pick dark direction + accent | ✅ Done — off-black + porcelain, contemporary justified grid |
| 1b — Gallery screen | ✅ Done (grid, Mixed/Grouped/Landscape/Portrait filters, immersive/justified, size slider/pinch, upload + drag-drop, status/now-showing badges) |
| 1b — Image detail screen | ✅ Done (E-ink ↔ Original, Info/Technical tabs, pinned On-display, actions, open-full-size) |
| 1b — Settings | ✅ Done and well beyond the sketch — consolidated tabbed modal, all config areas, custom dither editor |
| 1b — Frames/screens + server info | ✅ Done (frames drawer + server drawer) |
| 1b — per-image dither editor | ✅ Desktop-modal shell **merged into the main PC mockup** from [Plans/prototypes/desktop-modal-editor.html](../Plans/prototypes/desktop-modal-editor.html) (engine verbatim — kernels, tone chain, `DITHER_SCHEMA`, crop math, photo mat; masthead/demo-picker/explainer framing stripped). Entry points: **single-photo upload auto-opens it** (multi stays batch), and **⋯ → "Edit photo…"** on any healthy photo. Submit = checkmark → close → tile flips to Converting (real app: `POST /image/<name>/edit`). The **touch shell** goes in the phone/tablet artifact. Spec: [Plans/photo-edit-mode.md](../Plans/photo-edit-mode.md). |
| 1b — **phone/tablet** version | ✅ Done as a **responsive mode of the same artifact** (decision: one file, like production — no separate mobile fork). `@media (max-width: 700px)`: condensed header (dot pills, icon-only Upload, icon-only status chips), stacked drawers, horizontal-tab settings, stacked detail, 2-col diagnostics grid, and the per-image editor's controls become a **slide-up bottom bin** (touch-shell behavior) so the photo stays visible while adjusting. Custom global dither editor stays desktop-only (Custom… hidden ≤700px). Frame ⋯ always visible on touch. Verified 390×844 — no horizontal overflow on any surface; desktop unchanged. Review on-device via the artifact URL. |
| Step 2 — real static app consuming `/hokku/api/*` | ✅ **Shipped** at [`webserver/static/app/`](../webserver/static/app/) — no-build vanilla ES modules, served at `/hokku/static/app/index.html`. Gallery, photo detail + actions, header status/upload/failed, frames + diagnostics + server, all settings tabs, and the custom dither editor (live server preview) are wired to the live backend. Offline banner + `<img>` fallbacks + Escape handling done. Per-image crop/edit editor **deliberately deferred** (entry points omitted). Classic UI at `/hokku/ui` untouched; cutover is a future one-liner. |

**Deliberate divergences from the plan:** (1) PC artifact now, phone/tablet as a separate artifact;
(2) the deep dither pipeline moved from a global panel to a per-image editor concept.

**Design rules locked in:**
- Settings modals hold a fixed size, sized for the tallest tab — never resize as you toggle controls
  (reserve conditional space with `visibility`, not `display:none`).
- The **custom dither editor is desktop-only** — it's rarely edited and not worth a mobile layout.
- Header drawers (Server / Frames) are sticky toggles: they stay open until you click their own
  trigger; opening Settings does not close them; opening the Server drawer closes the Frames drawer.

---

## 2. Feature-gap review — old UI features not yet in the redesign

From a full inventory of `webserver/templates/index.html` + the API it calls. Excludes things already
matched (gallery, upload, detail, all config/dither settings, per-image actions).

### 🔴 Substantial gaps (real capabilities)
1. ✅ **Connected-screen diagnostics** — DONE. Click a frame card (or its ⋯ → Diagnostics) opens a
   modal: firmware (+update flag), boot/wake reason, uptime, battery V+%, Wi-Fi RSSI, free heap,
   **clock drift**, next-wake accuracy, and a **scrollable firmware log**. Stat cells flag warn/bad
   (low battery, weak RSSI, big drift, overdue).
2. ✅ **Remove / delete a frame** — DONE. ⋯ menu → Remove frame (also a Remove button in the
   diagnostics modal); splices `FRAMES`, re-renders drawer + gallery badges, toasts.
3. ✅ **Overdue-frame warning** — DONE. Amber "⚠ Overdue · <since>" banner across the bottom of the
   frame card thumbnail; card gets an amber ring; next-wake stat flags `bad` in diagnostics.
4. ✅ **Failed-conversions management** — DONE. Failed photos live in a **Failed-conversions modal**
   (not the gallery — the grid stays purely healthy photos): each row has a dimmed thumb, name,
   **error message**, **size**, per-row **Retry**; footer has **Retry all** / **Delete all** (two-step
   confirm). Reached via the dithering tile's dropdown → **Review** (failures show as an amber dot on
   the dithering tile while converting, or their own amber tile otherwise).
5. ✅ **Live system readouts** — DONE. Second row in the server drawer (reads as one unit with the
   identity row): **Disk free/total, CPU cores, free RAM (live), Workers ("Auto → 3 active"),
   Last served**; version row gained a **commit chip**.
6. ✅ **Global conversion progress** — DONE. Shown as a **header activity tile** ("Dithering 3 of 12 ·
   ~30s" + micro progress bar) and, expanded, in its dropdown row.

### 🟡 Smaller gaps (mostly display/polish)
7. ✅ **Debug-mode indicator** — DONE. Red entry in the status tile/dropdown while Debug screen is on,
   wired to the Server-settings Debug toggle (two-way).
8. **Status strip** — "Last served: X", ready/uploaded counts.
9. 🟠 **Per-image face-box overlay** — UI DONE, **backend hookup pending**. Photos with detected
   faces get a **Faces** pill in the detail view; toggling it shows red boxes (%-positioned so they
   track the render at any size), and the Technical tab shows the detected count. **The boxes in the
   mockup are placeholder values** — deliberately not polished further, since the mockup's canvas
   art has no real faces to align to. **Step 2 must:** read `face_bboxes` from `/hokku/api/status`
   (pixel coords in source-image space), convert to fractions of the *rendered* image (accounting
   for the panel crop/zoom-to-fill on the dithered render vs. the uncropped original), and
   **verify against real photos** that boxes land on the actual faces in both E-ink and Original
   views.
10. ✅ **Server time + version/commit** — DONE. Slim page footer (aligned to the content edges, hugs
    the viewport bottom): live-ticking **server time** left, **version + commit chip** right. The
    status strip's "Last served" (#8) lives in the server-drawer readouts.
11. ✅ **Upload progress** — DONE. Upload/drop takes over the status-tile face (per-file ticking +
    bar), ending in a green "N photos uploaded" that auto-clears. _(Replaces the single toast.)_

**Design rule (locked):** transient status/progress never pushes page content. It lives in compact
**Lightroom-style activity tiles** centred between the wordmark and the server pill — one tile per
activity (**upload first, then dithering**), each with a micro progress bar; failures fold into the
dithering tile as an amber dot. Click a tile for its detail dropdown; the failed row's Review opens
the failed-conversions modal. Full-width stacking banners were tried and rejected as jarring.
12. ✅ **Per-knob help popovers** — DONE. Every knob in the custom dither editor (all ~29, every
    control type) has a `DITHER_HELP` entry; hover or keyboard-focus the label → explanation popover
    (dotted underline affordance; desktop-only editor so hover is safe).
13. ✅ **Empty / first-run states** — DONE. Empty gallery (icon + "No photos yet" + upload guidance),
    per-filter empties ("No portrait photos yet."), frames drawer + Frames settings tab ("No frames
    connected yet — a frame appears here the first time it wakes up and checks in").
14. ✅ **Per-screen "match orientation"** — DONE. Toggle in the frame diagnostics modal ("Only queue
    photos matching this frame's orientation"), backed by `FRAMES[].match`.

### Notable non-gaps (deliberate)
- Old UI is a single **light "paper" theme** — no dark mode. The redesign *is* the dark version.
- Old UI has **no per-image dither override** (dither is global). The per-image editor is a new
  redesign capability (the "last big lift").

### Status
**The feature-gap punch list is complete** — all 14 items are ✅ done in the PC mockup (the only
deliberate leftover is #8's counts, folded into the server-drawer readouts as "Last served"). What
remains on the overall plan: the **phone/tablet artifact**, the **per-image dither editor** (last big
lift), and **Step 2** (the real static app at `webserver/static/app/`).
