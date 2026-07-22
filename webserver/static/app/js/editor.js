// editor.js — the per-image photo editor (#pedit-* overlay). Ported from the
// integrated editor in Plans/prototypes/app-mockup.html.
//
// The iOS-Photos crop engine (pan / zoom / rotate / aspect on a client canvas) is
// lifted verbatim — it's instant and needs no server. The dithered preview, though,
// is the REAL server render: POST /dither/preview with the crop + rotation + target
// orientation. (The mockup dithered in-browser, but that was a Euclidean approximation
// that ignored every LUT / colour-space control — the whole point of per-image dither —
// so the server is the single source of truth here, exactly like the Settings custom
// editor.) Open seeds from GET suggested_config; Submit posts to /image/<name>/edit.
// A photo uploaded via "Upload & edit" opens as an inert DRAFT that is DELETEd on cancel.

import { state, mutate } from "./state.js";
import { getSuggestedConfig, editorPreview, monoPreview, editImage, uploadDraft, deleteImage, originalUrl } from "./api.js";
import { $, $$, esc, toast, isMobileVp } from "./ui.js";

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const clone = (o) => JSON.parse(JSON.stringify(o));
function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a == null || b == null) return false;
  if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  if (typeof a === "object") { const ka = Object.keys(a), kb = Object.keys(b); return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k])); }
  return false;
}

// ── DOM refs (the overlay exists in index.html; this module is deferred) ──
const peditOverlay = $("#pedit-overlay");
const peditCard = $("#pedit-card");
const edStage = $("#edStage");
const cropCanvas = $("#cropCanvas");
const cropOverlay = $("#cropOverlay");
const dithWrap = $("#peditDithWrap");
const dithImg = $("#peditDith");
const beforeImg = $("#peditDithBefore");
const renderChip = $("#renderChip");
const mPanel = $("#mPanel");
const cropPanel = $("#cropPanel");
const groupPanel = $("#groupPanel");
const cropDock = $("#cropDock");
const toolRail = $("#toolRail");
const pTitle = $("#pTitle");
const pTitleText = $("#pTitleText");
const pReset = $("#pReset");
const moreMenu = $("#moreMenu");
const edSubmit = $("#edSubmit");
const edClassifier = $("#edClassifier");            // mobile: header strip
const edClassifierPanel = $("#edClassifierPanel");  // desktop: top of the controls panel

// ── editor state ──
// crop is kept in ROTATED-source PIXELS (drawCrop rewrites it every frame); it is
// normalised by the rotated-source dims only when talking to the server.
const ED = {
  name: null, isDraft: false, srcCanvas: null, base: null,
  mode: "crop", aspect: "H", rotation: 0, zoom: 1, panX: 0, panY: 0,
  crop: { x: 0, y: 0, w: 1, h: 1 }, cfg: null, stage: 0, panelOpen: false,
  // "Editing for" appearance axis. mono/monoBase are the per-image E1003 tone knob
  // set (short names) + the panel-wide default it inherits from. color = ED.cfg/ED.base.
  appearance: "color", mono: null, monoBase: null,
  // Crop model (Model A — primary + override): the live crop state (rotation/aspect/
  // zoom/pan) always lives in ED and belongs to whichever appearance is active. Spectra
  // is the PRIMARY. Mono FOLLOWS Spectra's crop by default (monoCropFollows=true); when
  // you "Frame separately" it forks its own snapshot into cropMono. On appearance switch
  // we snapshot the active crop out and restore the target's in.
  monoCropFollows: true,   // mono crop is inheriting the Spectra crop (no fork)
  colorCropSnap: null,     // saved Spectra crop while editing mono
  cropMono: null,          // the forked mono crop snapshot (null while following)
};
// a crop snapshot is everything drawCrop reconstructs the view from
function snapCrop() { return { rotation: ED.rotation, aspect: ED.aspect, zoom: ED.zoom, panX: ED.panX, panY: ED.panY }; }
function loadCrop(s) { ED.rotation = s.rotation; ED.aspect = s.aspect; ED.zoom = s.zoom; ED.panX = s.panX; ED.panY = s.panY; buildRotated(); syncAspectUI(); syncZoomUI(ED.zoom); drawCrop(); }
// Touching the crop while editing MONO forks it from Spectra (stops following). Called at
// the start of any deliberate mono crop edit. No-op in colour mode or if already forked.
function forkMonoCropIfEditing() {
  if (!isMono() || !ED.monoCropFollows) return;
  ED.monoCropFollows = false;
  updateMonoCropChip();
}
// Re-link mono to the Spectra crop (discard the fork) — the "Match Spectra" action.
function matchSpectraCrop() {
  ED.monoCropFollows = true; ED.cropMono = null;
  loadCrop(ED.colorCropSnap || snapCrop());
  updateMonoCropChip(); renderRail(); schedulePreview(0);
}
// The follow/fork state chip in the mono crop view. Following -> "[Frame separately]";
// forked -> "[↺ Match Spectra]". Hidden entirely in the colour appearance.
function updateMonoCropChip() {
  const chip = $("#monoCropChip"); if (!chip) return;
  if (!isMono()) { chip.hidden = true; return; }
  chip.hidden = false;
  const following = ED.monoCropFollows;
  chip.classList.toggle("forked", !following);
  $("#mccState").textContent = following ? "Following Spectra crop" : "Custom mono crop";
  $("#mccAction").textContent = following ? "Frame separately" : "↺ Match Spectra";
}
const _mccAction = $("#mccAction");
if (_mccAction) _mccAction.addEventListener("click", () => {
  if (ED.monoCropFollows) { forkMonoCropIfEditing(); renderRail(); }   // "Frame separately" (crop stays put; now editable independently)
  else { matchSpectraCrop(); }                                        // "↺ Match Spectra"
});
let rotatedSrc = null;   // ED.srcCanvas rotated by ED.rotation (built by buildRotated)

const target = () => (ED.aspect === "H" ? "landscape" : "portrait");
function normCrop() {
  const w = (rotatedSrc && rotatedSrc.width) || 1, h = (rotatedSrc && rotatedSrc.height) || 1;
  return { x: clamp(ED.crop.x / w, 0, 1), y: clamp(ED.crop.y / h, 0, 1), w: clamp(ED.crop.w / w, 0, 1), h: clamp(ED.crop.h / h, 0, 1) };
}

// ══════════════════════════ CROP ENGINE (lifted verbatim) ══════════════════════════
function buildRotated() {
  const base = ED.srcCanvas;
  if (ED.rotation % 2 === 0) { rotatedSrc = base; return; }
  const c = document.createElement("canvas"); c.width = base.height; c.height = base.width;
  const cx = c.getContext("2d"); cx.translate(c.width / 2, c.height / 2); cx.rotate(ED.rotation * Math.PI / 2);
  cx.drawImage(base, -base.width / 2, -base.height / 2); rotatedSrc = c;
}
function stageSize() { const r = edStage.getBoundingClientRect(); return { W: r.width, H: r.height }; }
function fitContain(sw, sh, bw, bh) { const s = Math.min(bw / sw, bh / sh); return { w: sw * s, h: sh * s, s }; }
let cropRect = null;
function syncZoomUI(v) { ["zoomSlider", "zoomSlider2"].forEach((id) => { const el = document.getElementById(id); if (el) el.value = v; }); }
function computeCropRect() {
  const { W, H } = stageSize(); const pad = 38;
  const ar = ED.aspect === "H" ? 4 / 3 : 3 / 4;
  const fit = fitContain(ar, 1, W - pad * 2, H - pad * 2);
  return { x: (W - fit.w) / 2, y: (H - fit.h) / 2, w: fit.w, h: fit.h, cx: W / 2, cy: H / 2 };
}
function initCrop() { buildRotated(); ED.zoom = 1; ED.panX = 0; ED.panY = 0; syncZoomUI(1); drawCrop(); }
// Feature 2: restore a SAVED crop (from suggested_config.edit_crop) by inverting
// drawCrop's math — reconstruct rotation/aspect/zoom/pan from the normalized rect so a
// re-opened photo shows exactly the crop it was submitted with (device-independent).
function restoreCrop(editCrop) {
  ED.rotation = (((editCrop.rotation_quarters || 0) % 4) + 4) % 4;
  ED.aspect = editCrop.target === "portrait" ? "V" : "H";
  buildRotated();
  const sw = rotatedSrc.width, sh = rotatedSrc.height;
  cropRect = computeCropRect();
  const F = cropRect;
  const rect = editCrop.rect || { x: 0, y: 0, w: 1, h: 1 };
  const cover = Math.max(F.w / sw, F.h / sh);
  ED.zoom = clamp(F.w / Math.max(1e-6, rect.w * sw) / cover, 1, 6);
  const eff = cover * ED.zoom;
  ED.panX = F.x - rect.x * eff * sw - F.cx + (sw * eff) / 2;
  ED.panY = F.y - rect.y * eff * sh - F.cy + (sh * eff) / 2;
  syncAspectUI();
  syncZoomUI(ED.zoom);
  drawCrop();   // clampPan + recompute ED.crop from the restored zoom/pan
}
function clampPan() {
  const cr = cropRect, sw = rotatedSrc.width, sh = rotatedSrc.height;
  const cover = Math.max(cr.w / sw, cr.h / sh), eff = cover * ED.zoom;
  const dw = sw * eff, dh = sh * eff;
  const mx = Math.max(0, (dw - cr.w) / 2), my = Math.max(0, (dh - cr.h) / 2);
  ED.panX = clamp(ED.panX, -mx, mx); ED.panY = clamp(ED.panY, -my, my);
  return { eff, dw, dh };
}
function drawHandles(o, fr) {
  o.strokeStyle = "rgba(243,242,239,.96)"; o.lineWidth = 3; o.lineCap = "round";
  const A = 18;
  [[fr.x, fr.y, 1, 1], [fr.x + fr.w, fr.y, -1, 1], [fr.x, fr.y + fr.h, 1, -1], [fr.x + fr.w, fr.y + fr.h, -1, -1]]
    .forEach(([x, y, sx, sy]) => { o.beginPath(); o.moveTo(x, y + sy * A); o.lineTo(x, y); o.lineTo(x + sx * A, y); o.stroke(); });
  const e = 13, mx = fr.x + fr.w / 2, my = fr.y + fr.h / 2;
  o.beginPath(); o.moveTo(mx - e, fr.y); o.lineTo(mx + e, fr.y); o.stroke();
  o.beginPath(); o.moveTo(mx - e, fr.y + fr.h); o.lineTo(mx + e, fr.y + fr.h); o.stroke();
  o.beginPath(); o.moveTo(fr.x, my - e); o.lineTo(fr.x, my + e); o.stroke();
  o.beginPath(); o.moveTo(fr.x + fr.w, my - e); o.lineTo(fr.x + fr.w, my + e); o.stroke();
  o.lineCap = "butt";
}
function drawCrop(frame) {
  const { W, H } = stageSize();
  cropRect = computeCropRect();
  const { eff, dw, dh } = clampPan();
  const F = cropRect, fr = frame || F;
  const left = F.cx + ED.panX - dw / 2, top = F.cy + ED.panY - dh / 2;
  ED.imgRect = { left, top, dw, dh, eff };
  cropCanvas.width = W; cropCanvas.height = H;
  const cx = cropCanvas.getContext("2d"); cx.clearRect(0, 0, W, H); cx.imageSmoothingQuality = "high";
  cx.drawImage(rotatedSrc, 0, 0, rotatedSrc.width, rotatedSrc.height, left, top, dw, dh);
  cropOverlay.width = W; cropOverlay.height = H;
  const o = cropOverlay.getContext("2d"); o.clearRect(0, 0, W, H);
  o.fillStyle = "rgba(9,10,14,.72)"; o.fillRect(0, 0, W, H);
  o.clearRect(fr.x, fr.y, fr.w, fr.h);
  o.strokeStyle = "rgba(243,242,239,.85)"; o.lineWidth = 1.5; o.strokeRect(fr.x, fr.y, fr.w, fr.h);
  o.strokeStyle = "rgba(243,242,239,.24)"; o.lineWidth = 1;
  for (let i = 1; i < 3; i++) {
    o.beginPath(); o.moveTo(fr.x + fr.w * i / 3, fr.y); o.lineTo(fr.x + fr.w * i / 3, fr.y + fr.h); o.stroke();
    o.beginPath(); o.moveTo(fr.x, fr.y + fr.h * i / 3); o.lineTo(fr.x + fr.w, fr.y + fr.h * i / 3); o.stroke();
  }
  drawHandles(o, fr);
  ED.crop = { x: (fr.x - left) / eff, y: (fr.y - top) / eff, w: fr.w / eff, h: fr.h / eff };
}
function hitHandle(px, py) {
  const fr = cropRect, t = 24;
  const nx = (x) => Math.abs(px - x) < t, ny = (y) => Math.abs(py - y) < t;
  const inX = px > fr.x + t && px < fr.x + fr.w - t, inY = py > fr.y + t && py < fr.y + fr.h - t;
  if (nx(fr.x) && ny(fr.y)) return "tl"; if (nx(fr.x + fr.w) && ny(fr.y)) return "tr";
  if (nx(fr.x) && ny(fr.y + fr.h)) return "bl"; if (nx(fr.x + fr.w) && ny(fr.y + fr.h)) return "br";
  if (ny(fr.y) && inX) return "t"; if (ny(fr.y + fr.h) && inX) return "b";
  if (nx(fr.x) && inY) return "l"; if (nx(fr.x + fr.w) && inY) return "r";
  return null;
}
function resizeTo(H, px, py) {
  const F = cropRect, ar = F.w / F.h, IR = ED.imgRect, min = 64;
  px = clamp(px, IR.left, IR.left + IR.dw); py = clamp(py, IR.top, IR.top + IR.dh);
  let x, y, w, h; const cx0 = F.cx, cy0 = F.cy;
  const corner = (ox, oy) => { let w2 = Math.abs(px - ox), h2 = Math.abs(py - oy); if (w2 / h2 > ar) h2 = w2 / ar; else w2 = h2 * ar; w = Math.max(min, w2); h = w / ar; x = px < ox ? ox - w : ox; y = py < oy ? oy - h : oy; };
  if (H === "br") corner(F.x, F.y); else if (H === "tl") corner(F.x + F.w, F.y + F.h);
  else if (H === "tr") corner(F.x, F.y + F.h); else if (H === "bl") corner(F.x + F.w, F.y);
  else if (H === "r") { w = Math.max(min, px - F.x); h = w / ar; x = F.x; y = cy0 - h / 2; }
  else if (H === "l") { const R = F.x + F.w; w = Math.max(min, R - px); h = w / ar; x = R - w; y = cy0 - h / 2; }
  else if (H === "b") { h = Math.max(min / ar, py - F.y); w = h * ar; y = F.y; x = cx0 - w / 2; }
  else { const B = F.y + F.h; h = Math.max(min / ar, B - py); w = h * ar; y = B - h; x = cx0 - w / 2; }
  x = Math.max(x, IR.left, F.x); y = Math.max(y, IR.top, F.y);
  w = Math.min(w, F.x + F.w - x); h = Math.min(h, F.y + F.h - y);
  return { x, y, w, h };
}
function snapBack(fr) {
  const F = cropRect, IR = ED.imgRect;
  const cover = Math.max(F.w / rotatedSrc.width, F.h / rotatedSrc.height);
  const zoom2 = clamp((IR.eff * (F.w / fr.w)) / cover, 1, 6), effc = cover * zoom2;
  const scx = (fr.x + fr.w / 2 - IR.left) / IR.eff, scy = (fr.y + fr.h / 2 - IR.top) / IR.eff;
  const dw2 = rotatedSrc.width * effc, dh2 = rotatedSrc.height * effc;
  const zx0 = ED.zoom, pX0 = ED.panX, pY0 = ED.panY, panX2 = dw2 / 2 - scx * effc, panY2 = dh2 / 2 - scy * effc;
  const t0 = performance.now(), D = 280;
  (function step() {
    const t = Math.min(1, (performance.now() - t0) / D), e = 1 - Math.pow(1 - t, 3);
    ED.zoom = zx0 + (zoom2 - zx0) * e; ED.panX = pX0 + (panX2 - pX0) * e; ED.panY = pY0 + (panY2 - pY0) * e;
    const lf = { x: fr.x + (F.x - fr.x) * e, y: fr.y + (F.y - fr.y) * e, w: fr.w + (F.w - fr.w) * e, h: fr.h + (F.h - fr.h) * e };
    drawCrop(t < 1 ? lf : undefined);
    if (t < 1) requestAnimationFrame(step); else { syncZoomUI(ED.zoom); renderRail(); }
  })();
}
function resetCrop() {
  if (!cropEdited()) return;
  if (ED.rotation !== 0) { ED.rotation = 0; initCrop(); renderRail(); return; }
  const z0 = ED.zoom, pX0 = ED.panX, pY0 = ED.panY, t0 = performance.now(), D = 260;
  (function step() {
    const t = Math.min(1, (performance.now() - t0) / D), e = 1 - Math.pow(1 - t, 3);
    ED.zoom = z0 + (1 - z0) * e; ED.panX = pX0 - pX0 * e; ED.panY = pY0 - pY0 * e; drawCrop();
    if (t < 1) requestAnimationFrame(step); else { ED.zoom = 1; ED.panX = 0; ED.panY = 0; syncZoomUI(1); drawCrop(); renderRail(); }
  })();
}
function cropEdited() { return ED.zoom > 1.001 || Math.abs(ED.panX) > 0.5 || Math.abs(ED.panY) > 0.5 || ED.rotation !== 0; }

const edPointers = new Map();
let panStart = null, pinchStart = null, rsz = null, lastCropTap = 0;
const ptDist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
function setZoom(z) { forkMonoCropIfEditing(); ED.zoom = clamp(z, 1, 6); syncZoomUI(ED.zoom); drawCrop(); renderRail(); }
cropOverlay.addEventListener("pointerdown", (e) => {
  if (ED.mode !== "crop") return;
  if (edPointers.size === 0) { const now = e.timeStamp; if (now - lastCropTap < 320) { lastCropTap = 0; resetCrop(); return; } lastCropTap = now; }
  forkMonoCropIfEditing();   // a drag/resize on the mono crop forks it from Spectra
  cropOverlay.setPointerCapture(e.pointerId);
  const r = cropOverlay.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  edPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (edPointers.size === 2) { const p = [...edPointers.values()]; pinchStart = { dist: ptDist(p[0], p[1]), zoom: ED.zoom }; panStart = null; rsz = null; return; }
  const H = hitHandle(px, py);
  if (H) { rsz = { H, frame: null }; } else { panStart = { panX: ED.panX, panY: ED.panY, x: e.clientX, y: e.clientY }; }
});
cropOverlay.addEventListener("pointermove", (e) => {
  if (!edPointers.has(e.pointerId)) return;
  const r = cropOverlay.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  edPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (edPointers.size >= 2 && pinchStart) { const p = [...edPointers.values()]; setZoom(pinchStart.zoom * ptDist(p[0], p[1]) / pinchStart.dist); return; }
  if (rsz) { const nf = resizeTo(rsz.H, px, py); rsz.frame = nf; drawCrop(nf); }
  else if (panStart) { ED.panX = panStart.panX + (e.clientX - panStart.x); ED.panY = panStart.panY + (e.clientY - panStart.y); drawCrop(); }
});
function endPtr(e) {
  edPointers.delete(e.pointerId); pinchStart = null;
  if (rsz) { const fr = rsz.frame; rsz = null; if (fr) snapBack(fr); else drawCrop(); }
  if (edPointers.size === 1) { const p = [...edPointers.values()][0]; panStart = { panX: ED.panX, panY: ED.panY, x: p.x, y: p.y }; }
  else if (edPointers.size === 0) { panStart = null; }
  renderRail();
}
cropOverlay.addEventListener("pointerup", endPtr);
cropOverlay.addEventListener("pointercancel", endPtr);
cropOverlay.addEventListener("wheel", (e) => { if (ED.mode !== "crop") return; e.preventDefault(); setZoom(ED.zoom * (e.deltaY < 0 ? 1.08 : 0.926)); }, { passive: false });

// aspect toggle + rotate wiring
const aspH = $("#aspH"), aspV = $("#aspV");
function syncAspectUI() {
  aspH.setAttribute("aria-pressed", ED.aspect === "H");
  aspV.setAttribute("aria-pressed", ED.aspect === "V");
  $("#aspIcoH").hidden = ED.aspect !== "H";
  $("#aspIcoV").hidden = ED.aspect !== "V";
}
function setAspect(a) { forkMonoCropIfEditing(); ED.aspect = a; syncAspectUI(); initCrop(); applyPanelState(); renderRail(); }
function rotBy(d) { forkMonoCropIfEditing(); ED.rotation = (ED.rotation + d) % 4; initCrop(); renderRail(); applyPanelState(); }
aspH.onclick = () => setAspect("H");
aspV.onclick = () => setAspect("V");
$("#aspToggle").onclick = () => setAspect(ED.aspect === "H" ? "V" : "H");
$("#rotL").onclick = () => rotBy(3);
$("#rotR").onclick = () => rotBy(1);
$("#rotL2").onclick = () => rotBy(3);
$("#rotR2").onclick = () => rotBy(1);
["zoomSlider", "zoomSlider2"].forEach((id) => {
  const el = document.getElementById(id);
  el.addEventListener("input", (e) => { setZoom(parseFloat(e.target.value)); });
  el.addEventListener("dblclick", resetCrop);
});

// ══════════════════════════ CURATED DITHER CONTROLS ══════════════════════════
// Same ImageConfig the server renders; the "base" (reset target + tweaked marker) is
// the classifier's suggested config we opened with — not a hardcoded default.
function getK(o, k) { if (k.indexOf(".") < 0) return o[k]; const p = k.split("."); return o[p[0]] ? o[p[0]][p[1]] : undefined; }
function setK(o, k, v) { if (k.indexOf(".") < 0) { o[k] = v; return; } const p = k.split("."); (o[p[0]] || (o[p[0]] = {}))[p[1]] = v; }
function fmt(v, step) { const d = step >= 1 ? 0 : step >= 0.05 ? 2 : 3; return Number(v).toFixed(d); }

// ── "Editing for" appearance (Color ⇄ E1003 Mono) ──
// The same rail/panel/rowChanged machinery drives either appearance via these; the
// active config/base/schema swap on ED.appearance. Mono uses a flat short-name knob
// set (matches the /dither/preview_mono payload + the per-image edit_mono blob).
const isMono = () => ED.appearance === "mono";
const actCfg = () => (isMono() ? ED.mono : ED.cfg);
const actBase = () => (isMono() ? ED.monoBase : ED.base);
const actSchema = () => (isMono() ? MONO_SCHEMA() : DITHER_SCHEMA(ED.cfg));
const hasMonoPanel = () => Object.values(state.status?.screens || {}).some((s) => s.panel_type === "mono16_e1003");

// ── dtcore mono model (matches the Settings "Custom E1003 Profile" editor) ──
// ED.mono / ED.monoBase carry the SHORT flat keys the /dither/preview_mono "custom"
// profile reads (profile + sharpen_* + sig_*/lc_*/basic_*), so what we send IS the
// effective knob set — no server merge needed. Formatters mirror settings.js exactly.
const mLR = (v) => (v > 0 ? "+" : "") + Math.round(v * 100);            // basic tonal ±100
const mEV = (v) => (v > 0 ? "+" : "") + (+v).toFixed(1) + " EV";        // exposure
const mP100 = (v) => Math.round(v * 100) + "%";                        // lc hi/sh 0..2 -> %
const mDetail = (v) => Math.round(v * 100 + 100) + "%";               // lc detail +100 offset
const mSk = (v) => (v > 0 ? "+" : "") + (+v).toFixed(2);
const mPct = (v) => Math.round(v) + "%", mPx = (v) => (+v).toFixed(2) + " px", mInt = (v) => String(Math.round(v));
const m2 = (v) => (+v).toFixed(2), m3 = (v) => (+v).toFixed(3), m2pct = (v) => (+v).toFixed(2) + "%", m4pct = (v) => (+v).toFixed(4) + "%";
// B&W Contrast preset (the dtcore defaults) — the starting point for a fresh Custom.
const MONO_DTCORE_DEFAULTS = {
  profile: "custom",
  sharpen_amount: 170, sharpen_radius: 1.2, sharpen_threshold: 2,
  sig_enabled: true, sig_contrast: 0.735, sig_skew: 1.0, sig_white: 100, sig_black: 0.7634,
  lc_enabled: true, lc_detail: 1.39, lc_highlights: 0.5, lc_shadows: 0.5, lc_midtone: 0.5,
  basic_enabled: true, basic_exposure: 0, basic_contrast: 0, basic_highlights: 0, basic_shadows: 0, basic_whites: 0, basic_blacks: 0, basic_clahe: 0,
};
// panel-wide mono default (Settings config) → the short-name payload the editor inherits
// from. Reads the mono_e1003_* dtcore config fields; falls back to the B&W Contrast values.
function monoPanelDefault() {
  const c = state.config?.config || {};
  const n = (k, d) => (c["mono_e1003_" + k] === undefined || c["mono_e1003_" + k] === null ? d : +c["mono_e1003_" + k]);
  const b = (k, d) => (c["mono_e1003_" + k] === undefined || c["mono_e1003_" + k] === null ? d : !!c["mono_e1003_" + k]);
  const D = MONO_DTCORE_DEFAULTS;
  return {
    profile: "custom",   // per-image editing is always Custom (presets stamp values in)
    sharpen_amount: n("sharpen_amount", D.sharpen_amount), sharpen_radius: n("sharpen_radius", D.sharpen_radius), sharpen_threshold: n("sharpen_threshold", D.sharpen_threshold),
    sig_enabled: b("sig_enabled", D.sig_enabled), sig_contrast: n("sig_contrast", D.sig_contrast), sig_skew: n("sig_skew", D.sig_skew), sig_white: n("sig_white", D.sig_white), sig_black: n("sig_black", D.sig_black),
    lc_enabled: b("lc_enabled", D.lc_enabled), lc_detail: n("lc_detail", D.lc_detail), lc_highlights: n("lc_highlights", D.lc_highlights), lc_shadows: n("lc_shadows", D.lc_shadows), lc_midtone: n("lc_midtone", D.lc_midtone),
    basic_enabled: b("basic_enabled", D.basic_enabled), basic_exposure: n("basic_exposure", D.basic_exposure), basic_contrast: n("basic_contrast", D.basic_contrast), basic_highlights: n("basic_highlights", D.basic_highlights), basic_shadows: n("basic_shadows", D.basic_shadows), basic_whites: n("basic_whites", D.basic_whites), basic_blacks: n("basic_blacks", D.basic_blacks), basic_clahe: n("basic_clahe", D.basic_clahe),
  };
}
// Only the dtcore short keys are valid under the custom profile; a stale FAITHFUL-shape
// edit_mono (darken_gamma/black_point/clarity/…) is dropped so old images open on the
// panel dtcore default rather than a broken mixed blob.
const MONO_DTCORE_KEYS = new Set(Object.keys(MONO_DTCORE_DEFAULTS));
const sanitizeMonoEdit = (e) => Object.fromEntries(Object.entries(e || {}).filter(([k]) => MONO_DTCORE_KEYS.has(k)));
// ••• Tone-preset menu: each entry stamps a dtcore knob set into ED.mono (always editable
// as Custom). "bw_contrast" is exact; "faithful" is a CLOSEST-dtcore APPROXIMATION of the
// legacy ramp look (sigmoid/local-contrast off, a mild Basic contrast lift) — NOT the real
// frozen Faithful pipeline, which has no dtcore representation.
const MONO_PRESETS = {
  bw_contrast: { ...MONO_DTCORE_DEFAULTS },
  faithful: {
    ...MONO_DTCORE_DEFAULTS,
    sig_enabled: false, lc_enabled: false, basic_enabled: true,
    basic_contrast: 0.15, basic_blacks: -0.06, basic_clahe: 0,
    sharpen_amount: 170, sharpen_radius: 1.2, sharpen_threshold: 2,
  },
};
// Stamp a preset (or the panel default) into ED.mono without disturbing profile/appearance,
// then refresh the panel + preview. "defaults" = the panel-wide Custom values (ED.monoBase).
function loadMonoPreset(which) {
  if (!ED.mono) return;
  const src = which === "defaults" ? ED.monoBase : MONO_PRESETS[which];
  if (!src) return;
  MONO_DTCORE_KEYS.forEach((k) => { if (k !== "profile" && k in src) ED.mono[k] = src[k]; });
  ED.mono.profile = "custom";
  refreshAdjust();
  if (isMono()) { renderMonoBefore(); schedulePreview(); }
}
function MONO_SCHEMA() {
  return [
    { group: "Tone", rows: [
      { t: "toggle", k: "basic_enabled", lab: "Basic (Lightroom)" },
      { t: "slider", k: "basic_exposure", lab: "Exposure", min: -5, max: 5, step: 1 / 3, neutral: 0, fmt: mEV },
      { t: "slider", k: "basic_contrast", lab: "Contrast", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mLR },
      { t: "slider", k: "basic_highlights", lab: "Highlights", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mLR },
      { t: "slider", k: "basic_shadows", lab: "Shadows", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mLR },
      { t: "slider", k: "basic_whites", lab: "Whites", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mLR },
      { t: "slider", k: "basic_blacks", lab: "Blacks", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mLR },
      { t: "slider", k: "basic_clahe", lab: "Local contrast (CLAHE)", min: 0, max: 5, step: 0.25, fmt: m2 },
    ] },
    { group: "Advanced Local Contrast", rows: [
      { t: "toggle", k: "sig_enabled", lab: "Sigmoid" },
      { t: "slider", k: "sig_contrast", lab: "Contrast", min: 0.7, max: 3, step: 0.001, neutral: 1.5, fmt: m3 },
      { t: "slider", k: "sig_skew", lab: "Skew", min: -1, max: 1, step: 0.01, neutral: 0, fmt: mSk },
      { t: "slider", k: "sig_white", lab: "Target white", min: 50, max: 100, step: 0.1, neutral: 100, fmt: m2pct },
      { t: "slider", k: "sig_black", lab: "Target black", min: 0, max: 5, step: 0.0001, neutral: 0, fmt: m4pct },
      { t: "toggle", k: "lc_enabled", lab: "Local contrast" },
      { t: "slider", k: "lc_detail", lab: "Detail", min: -1, max: 4, step: 0.01, neutral: 0.25, fmt: mDetail },
      { t: "slider", k: "lc_highlights", lab: "Highlights", min: 0, max: 2, step: 0.01, neutral: 0.5, fmt: mP100 },
      { t: "slider", k: "lc_shadows", lab: "Shadows", min: 0, max: 2, step: 0.01, neutral: 0.5, fmt: mP100 },
      { t: "slider", k: "lc_midtone", lab: "Midtone range", min: 0.001, max: 1, step: 0.005, neutral: 0.5, fmt: m3 },
    ] },
    { group: "Details", rows: [
      { t: "slider", k: "sharpen_amount", lab: "Sharpening", min: 0, max: 300, step: 5, fmt: mPct },
      { t: "slider", k: "sharpen_radius", lab: "Radius", min: 0.2, max: 3.0, step: 0.05, fmt: mPx },
      { t: "slider", k: "sharpen_threshold", lab: "Threshold", min: 0, max: 10, step: 1, fmt: mInt },
    ] },
  ];
}
function syncAppearanceUI() {
  $("#edAppearance").hidden = !hasMonoPanel();   // colour-only setups never see the switch
  $$("#appSeg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.app === ED.appearance)));
}
function setAppearance(a) {
  if (a === ED.appearance || !ED.name) return;
  const from = ED.appearance;
  ED.appearance = a;
  ED.stage = 0;
  endPeek(); pvReset();   // fresh view on each appearance
  // ── swap the crop workspace (Model A) ──
  if (from === "color" && a === "mono") {
    // leaving Spectra: remember its crop. Then show mono's framing:
    //  • following            -> a copy of the Spectra crop
    //  • forked, live snapshot -> ED.cropMono
    //  • forked, only saved    -> reconstruct from ED.savedCropMono (first entry)
    ED.colorCropSnap = snapCrop();
    if (ED.monoCropFollows) {
      loadCrop(ED.colorCropSnap);
    } else if (ED.cropMono) {
      loadCrop(ED.cropMono);
    } else if (ED.savedCropMono) {
      restoreCrop(ED.savedCropMono);
    } else {
      loadCrop(ED.colorCropSnap);
    }
  } else if (from === "mono" && a === "color") {
    // leaving mono: if forked, capture its live snapshot; restore Spectra's crop.
    if (!ED.monoCropFollows) ED.cropMono = snapCrop();
    if (ED.colorCropSnap) loadCrop(ED.colorCropSnap);
  }
  // swap the press-and-hold "before": mono = raw original, colour = raw crop
  // (reset beforeKey so renderBefore re-draws it on the next colour preview).
  if (isMono()) renderMonoBefore(); else beforeKey = "";
  syncAppearanceUI();
  renderRail();
  if (ED.mode === "dither" && ED.panelOpen) renderPanel();
  schedulePreview(0);
}
$("#appSeg").addEventListener("click", (e) => { const b = e.target.closest("[data-app]"); if (b) setAppearance(b.dataset.app); });
// the per-image mono override to persist: only knobs that differ from the panel
// default (null = fully inherit). Idempotent — re-saves an untouched override unchanged.
// When ANY knob differs we stamp profile:"custom" too, so the serve path's
// _effective_mono_kwargs routes this image through the dtcore custom branch (reading
// these very keys) regardless of the panel-wide profile.
function monoOverride() {
  if (!ED.mono || !ED.monoBase) return null;
  const diff = {};
  for (const k of Object.keys(ED.mono)) {
    if (k === "profile") continue;   // handled below (stamped only when something else changed)
    const a = ED.mono[k], b = ED.monoBase[k];
    const changed = (typeof a === "number" && typeof b === "number") ? Math.abs(a - b) > 1e-9 : a !== b;
    if (changed) diff[k] = a;
  }
  if (!Object.keys(diff).length) return null;
  diff.profile = "custom";
  return diff;
}
// The mono appearance's framing for the preview/render. Only a FORKED ("Frame separately")
// mono crop is sent; while following, we send NO crop so the mono render uses its own auto
// fit/rotate path — matching the server (which no longer inherits the colour crop). This is
// the client half of the regression revert; the future per-frame auto_rotate_fit policy will
// govern the un-cropped framing.
// the E1003 mono frame's configured orientation (portrait?), so the editor preview
// composes + shows content the way that frame will (upright, matching the panel).
function monoFramePortrait() {
  const screens = state.status?.screens || {};
  const s = Object.values(screens).find((sc) => sc.panel_type === "mono16_e1003");
  return !!s && s.orientation === "portrait";
}
function monoCropOpts() {
  // A forked mono crop sends its own framing; while following, send only the frame
  // orientation so the server's auto fit/rotate path frames for the E1003, returned upright.
  if (ED.monoCropFollows) return { frame_portrait: monoFramePortrait() };
  const nc = normCrop();
  return { rotation: ED.rotation, crop: [nc.x, nc.y, nc.w, nc.h], frame_portrait: monoFramePortrait() };
}
// Build a crop payload ({rotation_quarters, rect, target}) from a crop SNAPSHOT by loading
// it into the view (so normCrop() reads it), used at submit to serialize the forked mono
// crop regardless of which appearance is live. Mutates the view — caller must be done editing.
function cropPayloadFromSnap(snap) {
  if (!snap) return null;
  loadCrop(snap);
  return { rotation_quarters: ED.rotation, rect: normCrop(), target: (ED.aspect === "H" ? "landscape" : "portrait") };
}

const HUE_LUTS = ["hue_aware", "hue_aware_weighted", "oklab_hue_aware", "cam16ucs_hue_aware"];
const NEUTRAL_LUTS = ["hue_aware", "hue_aware_weighted", "cam16ucs_hue_aware"];
function DITHER_SCHEMA(c) {
  const lut = c && c.dither ? c.dither.lut_name : undefined;
  return [
    { group: "Tonal preparation", rows: [
      { t: "slider", k: "prepare_autocontrast_cutoff", lab: "Autocontrast cutoff", min: 0, max: 1, step: 0.05 },
      { t: "slider", k: "prepare_gamma", lab: "Gamma", min: 0.48, max: 1.28, step: 0.05 },
      { t: "slider", k: "prepare_midtone", lab: "Midtone lift", min: 0.62, max: 1.42, step: 0.05 },
      { t: "slider", k: "prepare_brightness", lab: "Brightness", min: 0.5, max: 1.5, step: 0.05 },
      { t: "slider", k: "prepare_contrast", lab: "Contrast", min: 0.6, max: 1.6, step: 0.05 },
      { t: "slider", k: "clahe_clip_limit", lab: "Local contrast (CLAHE)", min: 0, max: 3.5, step: 0.05 },
      { t: "slider", k: "clahe_keepout_feather", lab: "Keepout feather", min: 0, max: 0.03, step: 0.001 },
    ] },
    { group: "Colour enhancement", rows: [
      // The render applies color_enhance ONLY when adaptive saturation is off; when it's
      // on (the default/face pipelines) saturate_max_enhance is the real knob and
      // color_enhance is skipped. Bind one "Saturation" slider to whichever is live so it
      // always does something.
      {
        t: "slider",
        k: (c && c.adaptive_saturate_space && c.adaptive_saturate_space !== "off") ? "saturate_max_enhance" : "color_enhance",
        lab: "Saturation", min: 0.5, max: 2, step: 0.05,
      },
    ] },
    { group: "Palette LUT", rows: [
      { t: "chips", k: "dither.lut_name", lab: "LUT", opts: [["euclidean", "CIELAB"], ["euclidean_weighted", "CIELAB weighted"], ["hue_aware", "CIELAB hue-aware"], ["hue_aware_weighted", "CIELAB hue-aware weighted"], ["oklab", "OKLAB"], ["oklab_hue_aware", "OKLAB hue-aware"], ["cam16ucs", "CAM16-UCS"], ["cam16ucs_hue_aware", "CAM16-UCS hue-aware"], ["bw", "B&W only"]] },
      ...(HUE_LUTS.includes(lut) ? [{ t: "slider", k: "dither.hue_cutoff_deg", lab: "Hue cutoff °", min: 10, max: 180, step: 1 }] : []),
      ...(NEUTRAL_LUTS.includes(lut) ? [{ t: "slider", k: "dither.neutral_chroma", lab: "Neutral chroma", min: 0, max: 16, step: 0.5 }] : []),
    ] },
    { group: "Dither kernel", rows: [
      { t: "seg", k: "dither.algorithm", lab: "Algorithm", opts: [["floyd_steinberg", "Floyd–Steinberg"], ["atkinson", "Atkinson"], ["stucki", "Stucki"]] },
      { t: "toggle", k: "dither.serpentine", lab: "Serpentine scan" },
    ] },
  ];
}
const STAGE_SHORT = { "Tonal preparation": "Tonal", "Colour enhancement": "Color", "Palette LUT": "Palette", "Dither kernel": "Kernel",
  "Tone": "Tone", "Advanced Local Contrast": "Advanced", "Details": "Details" };

function rowChanged(row) { const v = getK(actCfg(), row.k), b = getK(actBase(), row.k); return row.t === "slider" ? Math.abs(v - b) > 1e-6 : v !== b; }
function groupEdited(gr) { return gr.rows.some(rowChanged); }

function renderPanel() {
  const schema = actSchema();
  if (ED.stage >= schema.length) ED.stage = 0;
  const gr = schema[ED.stage];
  pTitleText.textContent = STAGE_SHORT[gr.group] || gr.group;
  pReset.hidden = !groupEdited(gr); pReset.textContent = "Reset group";
  groupPanel.innerHTML = "";
  gr.rows.forEach((row) => groupPanel.appendChild(buildPanelRow(row)));
}
function buildPanelRow(row) {
  const cfg = actCfg(), base = actBase();   // color = ED.cfg/ED.base, mono = ED.mono/ED.monoBase
  const pct = (x) => ((clamp(x, row.min, row.max) - row.min) / (row.max - row.min)) * 100;
  const baseVal = getK(base, row.k);
  const fmtV = row.fmt || ((x) => fmt(x, row.step));   // dtcore rows carry a darktable/LR-style fmt
  if (row.t === "slider") {
    const el = document.createElement("div"); el.className = "prow slider" + (rowChanged(row) ? " tweaked" : "");
    // dtcore rows show a detent tick at the darktable factory default (row.neutral); the
    // colour rows have no neutral -> no tick (marker rendered via the --det CSS var).
    const det = row.neutral !== undefined ? '<div class="prow-det" style="left:' + pct(row.neutral) + '%"></div>' : "";
    const v = getK(cfg, row.k);
    el.innerHTML = '<div class="prow-head"><span class="prow-lab">' + row.lab + '</span><span class="prow-val">' + fmtV(v) + '</span></div>' +
      '<div class="prow-line">' + det + '<div class="prow-knob" style="left:' + pct(v) + '%"></div></div>';
    const valEl = el.querySelector(".prow-val"), knob = el.querySelector(".prow-knob");
    const setV = (nv) => {
      nv = Math.round(nv / row.step) * row.step;
      // magnetic snap: pull to the darktable neutral (dtcore rows) OR the inherited base,
      // whichever is within the ~2% band, so the tick physically grabs on drag.
      if (row.neutral !== undefined && Math.abs(nv - row.neutral) < (row.max - row.min) * 0.02) nv = row.neutral;
      else if (Math.abs(nv - baseVal) < (row.max - row.min) * 0.012) nv = baseVal;
      nv = clamp(nv, row.min, row.max);
      setK(cfg, row.k, nv);
      valEl.textContent = fmtV(nv); el.classList.toggle("tweaked", Math.abs(nv - baseVal) > 1e-6);
      knob.style.left = pct(nv) + "%"; schedulePreview(); renderRail();
    };
    let startX = 0, startV = 0, axis = 0, dragging = false, lastTap = 0;
    el.addEventListener("pointerdown", (e) => { const now = e.timeStamp; if (now - lastTap < 300) { setV(baseVal); lastTap = 0; return; } lastTap = now; startX = e.clientX; startV = getK(cfg, row.k); axis = 0; dragging = true; });
    el.addEventListener("pointermove", (e) => { if (!dragging) return; const dx = e.clientX - startX; if (axis === 0) { if (Math.abs(dx) < 6) return; axis = "x"; el.setPointerCapture(e.pointerId); el.classList.add("scrubbing"); } setV(startV + (dx / el.getBoundingClientRect().width) * (row.max - row.min)); });
    const end = () => { dragging = false; axis = 0; el.classList.remove("scrubbing"); };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end); el.addEventListener("lostpointercapture", end);
    el.addEventListener("dblclick", () => setV(baseVal));
    return el;
  }
  if (row.t === "seg") {
    const el = document.createElement("div"); el.className = "prow seg" + (rowChanged(row) ? " tweaked" : "");
    const v = getK(cfg, row.k);
    el.innerHTML = '<span class="prow-lab">' + row.lab + '</span><div class="ios-seg">' + row.opts.map((o) => '<button data-v="' + o[0] + '"' + (o[0] === v ? ' aria-pressed="true"' : '') + '>' + o[1] + '</button>').join("") + '</div>';
    el.querySelectorAll(".ios-seg button").forEach((b) => b.addEventListener("click", () => { setK(cfg, row.k, b.dataset.v); renderPanel(); schedulePreview(); renderRail(); }));
    return el;
  }
  if (row.t === "toggle") {
    const el = document.createElement("div"); el.className = "prow toggle" + (rowChanged(row) ? " tweaked" : "");
    const v = getK(cfg, row.k);
    el.innerHTML = '<span class="prow-lab">' + row.lab + '</span><button class="ios-switch' + (v ? ' on' : '') + '" role="switch" aria-checked="' + !!v + '"><span></span></button>';
    el.querySelector(".ios-switch").addEventListener("click", () => { setK(cfg, row.k, !getK(cfg, row.k)); renderPanel(); schedulePreview(); renderRail(); });
    return el;
  }
  if (row.t === "chips") {
    const el = document.createElement("div"); el.className = "prow menu" + (rowChanged(row) ? " tweaked" : "");
    const v = getK(cfg, row.k), sel = row.opts.find((o) => o[0] === v);
    el.innerHTML = '<span class="prow-lab">' + row.lab + '</span><button class="menu-btn"><span>' + (sel ? sel[1] : v) + '</span><svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>';
    el.querySelector(".menu-btn").addEventListener("click", (e) => openChoiceMenu(row, e.currentTarget));
    return el;
  }
}
function openChoiceMenu(row, btn) {
  const v = getK(ED.cfg, row.k);
  const back = document.createElement("div"); back.className = "choice-back";
  const menu = document.createElement("div"); menu.className = "choice-menu";
  menu.innerHTML = '<div class="choice-title">' + row.lab + '</div>' + row.opts.map((o) => '<button data-v="' + o[0] + '" class="' + (o[0] === v ? 'sel' : '') + '">' + o[1] + (o[0] === v ? '<span class="ck">✓</span>' : '') + '</button>').join("");
  peditCard.appendChild(back); peditCard.appendChild(menu);
  const mr = peditCard.getBoundingClientRect(), br = btn.getBoundingClientRect();
  let left = br.right - mr.left - 250, top = br.bottom - mr.top + 6;
  left = Math.max(8, left);
  if (top + menu.offsetHeight > mr.height - 8) top = Math.max(8, br.top - mr.top - menu.offsetHeight - 6);
  menu.style.left = left + "px"; menu.style.top = top + "px";
  const close = () => { back.remove(); menu.remove(); };
  back.addEventListener("click", close);
  menu.querySelectorAll("button[data-v]").forEach((b) => b.addEventListener("click", () => { setK(ED.cfg, row.k, b.dataset.v); close(); renderPanel(); schedulePreview(); renderRail(); }));
}
pReset.addEventListener("click", () => {
  if (ED.mode === "crop") { resetCrop(); applyPanelState(); return; }
  const cfg = actCfg(), base = actBase();   // mono resets to the panel-wide default
  const gr = actSchema()[ED.stage];
  gr.rows.forEach((row) => setK(cfg, row.k, getK(base, row.k)));
  renderPanel(); renderRail(); schedulePreview();
});

// ══════════════════════════ RAIL + PANEL ══════════════════════════
const CROP_ICON = '<svg viewBox="0 0 24 24"><path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M2 6h14a2 2 0 0 1 2 2v14"/></svg>';
const GROUP_ICON = {
  "Tonal preparation": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
  "Colour enhancement": '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 0 18c1.7 0 2-1.3 2-2.2 0-1.6 1.4-1.8 2.4-1.8H18a3 3 0 0 0 3-3c0-4.9-4-8-9-8z"/><circle cx="7.5" cy="10.5" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"/><circle cx="16.5" cy="10.5" r="1" fill="currentColor" stroke="none"/></svg>',
  "Palette LUT": '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>',
  "Dither kernel": '<svg viewBox="0 0 24 24"><path d="M4 4h4v4H4zM12 4h4v4h-4zM8 8h4v4H8zM16 8h4v4h-4zM4 12h4v4H4zM12 12h4v4h-4zM16 16h4v4h-4z" fill="currentColor" stroke="none"/></svg>',
  "Tone": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
  "Advanced Local Contrast": '<svg viewBox="0 0 24 24"><path d="M3 17l5-5 4 3 4-6 5 6"/><circle cx="8" cy="7" r="2"/></svg>',
  "Details": '<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"/></svg>',
};
function renderRail() {
  if (!ED.cfg) { toolRail.innerHTML = ""; return; }
  const schema = actSchema();
  // Crop is a photo-level step shown in BOTH appearances (Model A). In mono it edits the
  // mono crop (which follows Spectra until forked); the dot marks any crop deviation.
  let html =
    '<button class="rail-btn' + (ED.mode === "crop" ? " active" : "") + '" data-tool="crop">' + CROP_ICON + '<span class="rb-lab">Crop</span>' + (cropEdited() ? '<span class="rb-dot"></span>' : '') + '</button>'
    + '<div class="rail-sep"></div>';
  html += schema.map((gr, i) => {
    // highlighted only while its controls are actually open (so the default dither
    // view has NO group selected — just the preview)
    const active = ED.mode === "dither" && ED.panelOpen && ED.stage === i;
    const dot = groupEdited(gr) ? '<span class="rb-dot"></span>' : '';
    return '<button class="rail-btn' + (active ? " active" : "") + '" data-group="' + i + '">' + (GROUP_ICON[gr.group] || "") + '<span class="rb-lab">' + (STAGE_SHORT[gr.group] || gr.group) + '</span>' + dot + '</button>';
  }).join("");
  toolRail.innerHTML = html;
}
toolRail.addEventListener("click", (e) => {
  // crop toggles: tapping it while already cropping returns to the clean dither preview
  if (e.target.closest("[data-tool='crop']")) { setMode(ED.mode === "crop" ? "dither" : "crop"); return; }
  const gb = e.target.closest("[data-group]"); if (!gb) return;
  const i = +gb.dataset.group;
  if (ED.mode !== "dither") { setMode("dither"); ED.stage = i; openPanel(); return; }
  // re-tapping the active group hides its controls on mobile (toggle); desktop keeps it
  if (ED.panelOpen && ED.stage === i) { if (isMobileVp()) closePanel(); return; }
  if (ED.panelOpen) { ED.stage = i; renderPanel(); renderRail(); }
  else { ED.stage = i; openPanel(); }
});

let relayoutRAF = 0;
function startRelayout() {
  cancelAnimationFrame(relayoutRAF);
  const t0 = performance.now(), D = 320;
  (function step() {
    if (peditOverlay.hidden) return;
    if (ED.mode === "crop") drawCrop();
    if (performance.now() - t0 < D) relayoutRAF = requestAnimationFrame(step);
  })();
}
function applyPanelState() {
  const touch = isMobileVp();
  // Desktop: the panel stays open — it hosts the always-visible classifier badge, and a
  // fixed width means the crop canvas never re-fits mid-mode. Mobile: the bin opens only
  // for dither controls; crop uses the dock.
  const open = !touch || (ED.mode === "dither" && ED.panelOpen);
  const wasOpen = mPanel.classList.contains("open");
  const dockShow = ED.mode === "crop" && touch, wasDock = !cropDock.hidden;
  mPanel.classList.toggle("open", open);
  cropDock.hidden = !dockShow;
  // controls show only when a tool is chosen; the default dither view shows just the badge
  const showCrop = ED.mode === "crop" && !touch;
  const showGroup = ED.mode === "dither" && ED.panelOpen;
  cropPanel.hidden = !showCrop;
  groupPanel.hidden = !showGroup;
  pTitle.hidden = !(showCrop || showGroup);
  if (showCrop) { pTitleText.textContent = "Crop"; pReset.hidden = !cropEdited(); updateMonoCropChip(); }
  else if (showGroup) renderPanel();
  if (open !== wasOpen || dockShow !== wasDock) startRelayout();
}
function openPanel() { ED.panelOpen = true; applyPanelState(); renderRail(); }
function closePanel() { ED.panelOpen = false; applyPanelState(); renderRail(); }

function setMode(m) {
  ED.mode = m;
  pvReset();   // preview zoom is per-mode; reset when entering crop/dither
  const crop = m === "crop";
  cropCanvas.style.display = crop ? "block" : "none";
  cropOverlay.style.display = crop ? "block" : "none";
  dithWrap.hidden = crop;
  if (crop) { ED.panelOpen = false; applyPanelState(); renderRail(); drawCrop(); }
  else { applyPanelState(); renderRail(); schedulePreview(0); }
}

// slide-up bin (touch): tap the stage or the grabber to close it
let suppressStageClick = false;
edStage.addEventListener("click", () => {
  if (suppressStageClick) { suppressStageClick = false; return; }
  if (isMobileVp() && ED.mode === "dither" && ED.panelOpen) closePanel();
});
$("#binGrabber").addEventListener("click", () => { if (ED.panelOpen) closePanel(); });

// press-and-hold the dithered preview to peek at the un-dithered original (before/after).
// a quick tap must NOT flash it — only a sustained hold swaps in the "before"; release restores.
const PEEK_HOLD_MS = 280, PEEK_MOVE_TOL = 12;
let peekTimer = null, peekAt = null, peeking = false;
// ── preview zoom/pan: inspect the render larger (wheel/pinch to zoom, drag/pinch to
// pan). The editor UI itself never zooms (touch-action on .pedit-card); only the
// preview stage does. Transform rides on #peditDithWrap so the peek "before" stays
// pixel-aligned with the "after". ──
const MAX_PV_ZOOM = 4;
let pvZoom = 1, pvX = 0, pvY = 0, panStartX = 0, panStartY = 0;
let pinching = false, pinchDist = 0, pinchCx = 0, pinchCy = 0;
function applyPv() {
  dithWrap.style.transform = pvZoom === 1 ? "" : `translate(${pvX}px,${pvY}px) scale(${pvZoom})`;
  edStage.classList.toggle("pv-zoomed", pvZoom > 1);
}
function pvReset() { pvZoom = 1; pvX = 0; pvY = 0; applyPv(); }
function pvClamp() {   // keep the scaled image from panning entirely out of view
  const bw = dithImg.offsetWidth, bh = dithImg.offsetHeight, sw = edStage.clientWidth, sh = edStage.clientHeight;
  const mx = Math.max(0, (bw * pvZoom - sw) / 2), my = Math.max(0, (bh * pvZoom - sh) / 2);
  pvX = clamp(pvX, -mx, mx); pvY = clamp(pvY, -my, my);
}
function pvZoomAt(cx, cy, factor) {   // zoom toward a point (cursor / pinch centre)
  const nz = clamp(pvZoom * factor, 1, MAX_PV_ZOOM);
  if (nz === pvZoom) return;
  const s = edStage.getBoundingClientRect();
  const ox = cx - s.left - s.width / 2, oy = cy - s.top - s.height / 2;
  pvX = ox - (ox - pvX) * (nz / pvZoom); pvY = oy - (oy - pvY) * (nz / pvZoom);
  pvZoom = nz;
  if (pvZoom === 1) { pvX = 0; pvY = 0; }
  pvClamp(); applyPv();
}
function cancelPeek() {   // hide the peek but keep the pointer session (so a drag can pan)
  clearTimeout(peekTimer); peekTimer = null;
  if (peeking) { peeking = false; suppressStageClick = true; dithWrap.classList.remove("peek"); }
}
function endPeek() { cancelPeek(); peekAt = null; }
edStage.addEventListener("pointerdown", (e) => {
  if (ED.mode !== "dither" || pinching) return;
  peekAt = { x: e.clientX, y: e.clientY }; panStartX = pvX; panStartY = pvY;
  try { edStage.setPointerCapture(e.pointerId); } catch (_) {}
  clearTimeout(peekTimer);
  // press-and-hold peek: only arm when there's a render to A/B against
  if (previewUrl && !dithImg.classList.contains("broken")) {
    peekTimer = setTimeout(() => { peeking = true; dithWrap.classList.add("peek"); }, PEEK_HOLD_MS);
  }
});
edStage.addEventListener("pointermove", (e) => {
  if (pinching || !peekAt) return;
  const dx = e.clientX - peekAt.x, dy = e.clientY - peekAt.y;
  if (Math.hypot(dx, dy) > PEEK_MOVE_TOL) cancelPeek();   // a drag is not a peek
  if (pvZoom > 1 && !peeking) { pvX = panStartX + dx; pvY = panStartY + dy; pvClamp(); applyPv(); }
});
["pointerup", "pointercancel", "pointerleave"].forEach((ev) => edStage.addEventListener(ev, endPeek));
// desktop wheel = zoom toward the cursor. Zoom PROPORTIONAL to the normalized scroll
// delta, clamped per event: devices that fire many small wheel events per flick
// (free-spin / high-res mice, trackpads) zoom smoothly instead of snapping, and a
// single large delta can't jump straight to max.
edStage.addEventListener("wheel", (e) => {
  if (ED.mode !== "dither") return;
  e.preventDefault();
  let d = e.deltaY;
  if (e.deltaMode === 1) d *= 16;                              // LINE → ~px
  else if (e.deltaMode === 2) d *= (edStage.clientHeight || 400);   // PAGE → px
  d = clamp(d, -100, 100);
  pvZoomAt(e.clientX, e.clientY, Math.exp(-d * 0.003));
}, { passive: false });
// mobile 2-finger pinch = zoom + pan (page-zoom is already blocked by touch-action)
edStage.addEventListener("touchstart", (e) => {
  if (ED.mode !== "dither" || e.touches.length !== 2) return;
  pinching = true; cancelPeek(); peekAt = null;
  const [a, b] = e.touches;
  pinchDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  pinchCx = (a.clientX + b.clientX) / 2; pinchCy = (a.clientY + b.clientY) / 2;
  e.preventDefault();
}, { passive: false });
edStage.addEventListener("touchmove", (e) => {
  if (!pinching || e.touches.length !== 2) return;
  e.preventDefault();
  const [a, b] = e.touches;
  const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
  if (pinchDist > 0) pvZoomAt(cx, cy, d / pinchDist);
  pvX += cx - pinchCx; pvY += cy - pinchCy; pvClamp(); applyPv();
  pinchDist = d; pinchCx = cx; pinchCy = cy;
}, { passive: false });
edStage.addEventListener("touchend", (e) => { if (e.touches.length < 2) pinching = false; });
// block the native long-press/drag reactions over the preview so only our peek shows:
// iOS callout/loupe + desktop right-click menu (contextmenu) and the drag-to-select / drag-image (dragstart)
edStage.addEventListener("contextmenu", (e) => e.preventDefault());
edStage.addEventListener("dragstart", (e) => e.preventDefault());

// ══════════════════════════ SERVER PREVIEW (debounced, one in flight) ══════════════════════════
let previewTimer = null, previewCtrl = null, previewUrl = null, previewKey = null;
function schedulePreview(delay = 260) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, delay);
}
async function runPreview() {
  if (!ED.name || ED.mode !== "dither") return;
  // Dedupe: skip the round-trip when the effective render request is unchanged (detent
  // snaps, dbl-click resets to the current value, switching a tool without editing). Key
  // captures everything the render depends on for the active appearance.
  const key = isMono()
    ? "mono|" + ED.name + "|" + JSON.stringify(ED.mono) + "|" + target() + "|" + ED.rotation + "|" + JSON.stringify(normCrop())
    : "color|" + ED.name + "|" + target() + "|" + ED.rotation + "|" + JSON.stringify(normCrop()) + "|" + JSON.stringify(ED.cfg);
  if (key === previewKey && previewUrl) return;
  if (previewCtrl) previewCtrl.abort();
  previewCtrl = new AbortController();
  renderChip.classList.add("show");
  try {
    let blobUrl;
    if (isMono()) {
      // The grayscale E1003 render IS the mode signal. ED.mono is the tone payload;
      // the mono framing (its own crop, following Spectra until forked) rides alongside.
      blobUrl = await monoPreview(ED.name, ED.mono, previewCtrl.signal, monoCropOpts());
    } else {
      const nc = normCrop();
      renderBefore();
      ({ blobUrl } = await editorPreview(ED.name, ED.cfg, { orientation: target(), crop: [nc.x, nc.y, nc.w, nc.h], rotation: ED.rotation }, previewCtrl.signal));
    }
    previewKey = key;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = blobUrl;
    dithImg.classList.remove("broken"); dithImg.src = blobUrl;
    renderChip.classList.remove("show");
  } catch (e) {
    if (e.name === "AbortError") return;
    renderChip.classList.remove("show");
    dithImg.classList.add("broken");
    toast("Preview failed: " + e.message);
  }
}

// client-side "before": the raw original cropped/rotated to the SAME frame as the dithered
// preview (ED.crop is already in rotatedSrc pixels), so press-and-hold shows a pixel-aligned
// before/after with no server round-trip. Re-rendered only when the crop or rotation changes.
let beforeKey = "";
function renderBefore() {
  if (!rotatedSrc || !ED.crop) return;
  const cr = ED.crop;
  const key = ED.rotation + ":" + cr.x + ":" + cr.y + ":" + cr.w + ":" + cr.h;
  if (key === beforeKey) return;
  beforeKey = key;
  const MAX = 900, scale = Math.min(1, MAX / Math.max(cr.w, cr.h));
  const cw = Math.max(1, Math.round(cr.w * scale)), ch = Math.max(1, Math.round(cr.h * scale));
  const c = document.createElement("canvas");
  c.width = cw; c.height = ch;
  c.getContext("2d").drawImage(rotatedSrc, cr.x, cr.y, cr.w, cr.h, 0, 0, cw, ch);
  beforeImg.src = c.toDataURL("image/jpeg", 0.9);
}
// Mono "before": the ORIGINAL photo in grayscale with NO tone editing — every dtcore
// stage disabled, so render_tone is a passthrough (just the E1003 fit + dither). This
// A/Bs your edits against the untouched source, so you can judge how far the pipeline
// (and your tweaks) have moved the image. Cached per photo; shares #peditDithBefore.
const MONO_RAW_PAYLOAD = () => ({
  profile: "custom",
  sharpen_amount: 0, sharpen_radius: 1.2, sharpen_threshold: 2,   // no sharpening on the raw ref
  sig_enabled: false, sig_contrast: 0.735, sig_skew: 1.0, sig_white: 100, sig_black: 0.7634,
  lc_enabled: false, lc_detail: 1.39, lc_highlights: 0.5, lc_shadows: 0.5, lc_midtone: 0.5,
  basic_enabled: false, basic_exposure: 0, basic_contrast: 0, basic_highlights: 0, basic_shadows: 0, basic_whites: 0, basic_blacks: 0, basic_clahe: 0,
});
let monoBeforeKey = null, monoBeforeUrl = null, monoBeforeCtrl = null;
async function renderMonoBefore() {
  if (!ED.name) return;
  const opts = monoCropOpts();
  // Cache per (photo + framing): the raw "before" must reframe when the mono crop changes,
  // so before/after A/B the SAME crop (only the tone edits differ).
  const ck = ED.name + "|" + opts.rotation + "|" + JSON.stringify(opts.crop);
  if (monoBeforeUrl && monoBeforeKey === ck) { beforeImg.src = monoBeforeUrl; return; }
  monoBeforeKey = ck;
  if (monoBeforeCtrl) monoBeforeCtrl.abort();
  monoBeforeCtrl = new AbortController();
  try {
    const url = await monoPreview(ED.name, MONO_RAW_PAYLOAD(), monoBeforeCtrl.signal, opts);
    if (monoBeforeUrl) URL.revokeObjectURL(monoBeforeUrl);
    monoBeforeUrl = url; beforeImg.src = url;
  } catch (e) { if (e.name !== "AbortError") monoBeforeKey = null; }
}

// ══════════════════════════ PRESETS / AUTO / RESET (••• menu) ══════════════════════════
// The ••• presets are the three pipeline presets, labelled by pipeline (Default / Face /
// B&W). Applying one swaps in that pipeline's full ImageConfig from the server presets
// (keeping the crop untouched).
function presetCfg(name) {
  const presets = state.config ? state.config.dither_presets || {} : {};
  if (presets[name]) {
    const { label, description, ...cfg } = presets[name];
    return clone(cfg);
  }
  return clone(ED.base);  // fallback if presets aren't loaded yet
}
function refreshAdjust() { renderRail(); if (ED.mode === "dither" && ED.panelOpen) renderPanel(); }
// the ••• preset menu lives in two spots — the header toolbar (mobile) and on the
// classifier/preset badge in the controls panel (desktop). Both drive the same menu; it
// pops up under whichever ••• was clicked.
function positionMoreMenu(btn) {
  const cardR = peditCard.getBoundingClientRect(), br = btn.getBoundingClientRect();
  moreMenu.style.top = br.bottom - cardR.top + 6 + "px";
  const menuW = moreMenu.offsetWidth || 240;
  if (br.right - menuW < cardR.left + 8) {   // right-aligning would overflow the left edge
    moreMenu.style.right = "auto"; moreMenu.style.left = br.left - cardR.left + "px";
  } else {
    moreMenu.style.left = "auto"; moreMenu.style.right = cardR.right - br.right + "px";
  }
}
$$(".ed-more").forEach((btn) => btn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (moreMenu.hidden) { syncMoreMenuSections(); moreMenu.hidden = false; positionMoreMenu(btn); } else moreMenu.hidden = true;
}));
document.addEventListener("click", (e) => { if (!moreMenu.hidden && !moreMenu.contains(e.target) && !e.target.closest(".ed-more")) moreMenu.hidden = true; });
moreMenu.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => { ED.cfg = presetCfg(b.dataset.preset); moreMenu.hidden = true; refreshAdjust(); if (ED.mode === "dither") schedulePreview(); }));
$("#mmAuto").addEventListener("click", () => { ED.cfg = clone(ED.base); moreMenu.hidden = true; refreshAdjust(); if (ED.mode === "dither") schedulePreview(); });
$("#mmReset").addEventListener("click", () => { ED.cfg = clone(ED.base); moreMenu.hidden = true; refreshAdjust(); if (ED.mode === "dither") schedulePreview(); });
// mono tone presets (stamp dtcore knobs into ED.mono) + reset-to-panel-defaults
moreMenu.querySelectorAll("[data-mono-preset]").forEach((b) => b.addEventListener("click", () => { loadMonoPreset(b.dataset.monoPreset); moreMenu.hidden = true; }));
$("#mmMonoReset").addEventListener("click", () => { loadMonoPreset("defaults"); moreMenu.hidden = true; });
// the ••• menu shows the colour preset section or the mono tone-preset section per appearance
function syncMoreMenuSections() {
  const mono = isMono();
  const cg = $("#mmColorGrp"), mg = $("#mmMonoGrp");
  if (cg) cg.hidden = mono;
  if (mg) mg.hidden = !mono;
}

// ══════════════════════════ OPEN / SUBMIT / CLOSE ══════════════════════════
function loadSource(name) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const MAX = 1800;
      const s = Math.min(1, MAX / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.naturalWidth * s));
      c.height = Math.max(1, Math.round(img.naturalHeight * s));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      resolve(c);
    };
    img.onerror = () => reject(new Error("could not load image"));
    img.src = originalUrl({ name });
  });
}

// classifier auto-decision badge (shown under the header): which of the three
// pipelines the server picked, so the editor's starting point is visible.
const PIPE_LABEL = { bw: "Black & white", face: "Portrait", default: "Standard photo" };
const PIPE_TIP = {
  bw: "The server detected a black & white photo and chose the B&W pipeline (skips the colour boosting that would tint grays). Your edits start from here.",
  face: "The server detected faces and chose the portrait pipeline (tuned for natural skin tones; faces are shielded from local-contrast). Your edits start from here.",
  default: "No black & white or faces detected — the server chose the standard pipeline. Your edits start from here.",
};
// the dither preset the chosen pipeline resolves to (deep-equality against the server's
// named presets, same rule as Settings), or null when it's a custom pipeline
function matchPresetLabel(cfg) {
  if (!cfg) return null;
  const presets = state.config ? state.config.dither_presets || {} : {};
  for (const [k, p] of Object.entries(presets)) {
    const { label, description, ...pcfg } = p;
    if (Object.keys(pcfg).every((key) => deepEqual(cfg[key], pcfg[key]))) return label || k;
  }
  return null;
}
function setClassifierBadge(sc) {
  const p = PIPE_LABEL[sc.pipeline] ? sc.pipeline : "default";
  const faces = (sc.face_bboxes || []).length;
  let sub = "";
  if (p === "face" && faces) sub = faces + (faces === 1 ? " face" : " faces") + (sc.faces_protected ? " · protected" : "");
  const preset = matchPresetLabel(sc.image_config);
  const html =
    '<span class="edc-dot edc-' + p + '" aria-hidden="true"></span>' +
    '<span class="edc-lab">Auto</span><b class="edc-name">' + PIPE_LABEL[p] + "</b>" +
    '<span class="edc-preset">' + esc(preset || "Custom") + "</span>" +
    (sub ? '<span class="edc-sub">' + esc(sub) + "</span>" : "");
  // fill both slots; CSS shows the header strip on mobile, the in-panel badge on desktop
  for (const el of [edClassifier, edClassifierPanel]) {
    el.innerHTML = html;
    el.title = PIPE_TIP[p];
    el.hidden = false;
  }
}

// Open the editor on an existing image (isDraft=false) or a freshly uploaded draft.
export async function openEditor(name, { isDraft = false } = {}) {
  edClassifier.hidden = edClassifierPanel.hidden = true;   // clear stale badges until suggested_config lands
  let sc;
  try { sc = await getSuggestedConfig(name); }
  catch (e) { toast("Could not open editor: " + e.message); if (isDraft) deleteImage(name).catch(() => {}); return; }
  ED.name = name; ED.isDraft = isDraft;
  // base = the classifier's live decision (the Auto/Reset target); cfg = the saved edit
  // when there is one (Feature 2: remember edits), else Auto.
  ED.base = clone(sc.image_config); ED.base.dither = ED.base.dither || {};
  ED.cfg = sc.edit_image_config ? clone(sc.edit_image_config) : clone(ED.base);
  ED.cfg.dither = ED.cfg.dither || {};
  // mono appearance: the panel-wide default is the base/inherit target; overlay any
  // saved per-image edit_mono on top (stale faithful-shape keys are dropped). Always
  // start on Color.
  ED.monoBase = monoPanelDefault();
  ED.mono = { ...ED.monoBase, ...sanitizeMonoEdit(sc.edit_mono), profile: "custom" };
  // mono crop model: a saved edit_crop_mono means the panel was FORKED; else it follows.
  ED.savedCropMono = sc.edit_crop_mono || null;
  ED.monoCropFollows = !ED.savedCropMono;
  ED.cropMono = null; ED.colorCropSnap = null;
  ED.appearance = "color";
  ED.aspect = sc.orientation === "portrait" ? "V" : "H";
  ED.rotation = 0; ED.zoom = 1; ED.panX = 0; ED.panY = 0; ED.stage = 0; ED.panelOpen = false; ED.mode = "dither";
  moreMenu.hidden = true;
  dithImg.removeAttribute("src"); previewKey = null; if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  syncAspectUI();
  syncAppearanceUI();
  setClassifierBadge(sc);
  peditOverlay.hidden = false;
  renderChip.classList.add("show");
  try { ED.srcCanvas = await loadSource(name); }
  catch (e) { renderChip.classList.remove("show"); toast(e.message); closeEditor(); return; }
  renderChip.classList.remove("show");
  // open on the dithered preview with NO tool selected (clean starting view); restore a
  // saved crop when re-editing (Feature 2), else start from the default cover crop
  requestAnimationFrame(() => { if (sc.edit_crop) restoreCrop(sc.edit_crop); else initCrop(); setMode("dither"); });
}

edSubmit.addEventListener("click", async () => {
  if (edSubmit.dataset.busy) return;
  edSubmit.dataset.busy = "1";
  const orig = edSubmit.innerHTML;
  edSubmit.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M5 13l4 4L19 7"/></svg>';
  const name = ED.name;
  const image = deepEqual(ED.cfg, ED.base) ? null : ED.cfg;
  // Crop payloads must come from each appearance's OWN state, not whichever is live.
  // Capture the forked mono crop from its live view first (if we're in mono), then
  // return to Spectra so normCrop()/target() read the PRIMARY colour crop.
  if (isMono() && !ED.monoCropFollows) ED.cropMono = snapCrop();
  const monoForked = !ED.monoCropFollows;
  const monoSnap = monoForked ? (ED.cropMono || snapCrop()) : null;
  if (isMono()) setAppearance("color");   // now ED holds the Spectra crop
  const editCrop = { rotation_quarters: ED.rotation, rect: normCrop(), target: target() };
  // per-image mono override (only sent when a mono panel exists, so colour-only setups
  // are untouched); null clears it, undefined leaves it alone.
  const mono = hasMonoPanel() ? monoOverride() : undefined;
  // mono crop override: the forked framing (built from its snapshot), or null = follow.
  const editCropMono = hasMonoPanel()
    ? (monoForked ? cropPayloadFromSnap(monoSnap) : null)
    : undefined;
  try {
    await editImage(name, image, editCrop, mono, editCropMono);
    ED.isDraft = false;   // a submitted draft is now a real pending image — don't delete it
    closeEditor();
    mutate();
    toast("Submitted — the server is rendering " + name + "…");
  } catch (e) {
    toast("Submit failed: " + e.message);
  } finally {
    edSubmit.innerHTML = orig; delete edSubmit.dataset.busy;
  }
});

function closeEditor() {
  peditOverlay.hidden = true; moreMenu.hidden = true; cropDock.hidden = true;
  peditCard.querySelectorAll(".choice-back,.choice-menu").forEach((n) => n.remove());
  clearTimeout(previewTimer); if (previewCtrl) previewCtrl.abort();
  renderChip.classList.remove("show");
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  endPeek(); beforeKey = ""; beforeImg.removeAttribute("src");
  monoBeforeName = null; if (monoBeforeCtrl) monoBeforeCtrl.abort();
  if (monoBeforeUrl) { URL.revokeObjectURL(monoBeforeUrl); monoBeforeUrl = null; }
  const draftToDrop = ED.isDraft ? ED.name : null;
  ED.name = null; ED.srcCanvas = null; rotatedSrc = null; ED.isDraft = false;
  if (draftToDrop) deleteImage(draftToDrop).catch(() => {});   // discard the inert draft
}
$("#edCancel").addEventListener("click", closeEditor);
peditOverlay.addEventListener("click", (e) => { if (e.target === peditOverlay) closeEditor(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !peditOverlay.hidden) { e.stopPropagation(); closeEditor(); } });

// Upload ONE file and open it in the editor as a draft (never enters rotation unless
// submitted). Used by the header's "Upload & edit" single-file path.
export async function uploadAndEdit(file) {
  toast("Uploading " + (file.name || "photo") + "…");   // feedback during the (blocking) upload + analyze
  try {
    const { name } = await uploadDraft(file);
    await openEditor(name, { isDraft: true });
  } catch (e) { toast("Upload failed: " + e.message); }
}

// keep the crop framing correct when the window resizes
window.addEventListener("resize", () => { if (peditOverlay.hidden) return; if (ED.mode === "crop") drawCrop(); });
