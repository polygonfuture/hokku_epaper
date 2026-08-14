// photo.js — per-photo surfaces ported from the mockup (menuItemsHTML / runAction /
// openSheet / openMenu / detailHTML / tile gestures). The semantic changes vs. the
// mockup: tiles carry a data-name and we look the entry up live from the store (no
// stale snapshot), canvases become <img> (/dithered, /original), and every action
// hits the real backend. The per-image editor entry ("Edit photo…") stays hidden
// (deferred), and the per-frame "Send to <frame>" list calls the M1 endpoint.

import { state, subscribe, mutate } from "./state.js";
import { deleteImage, retryImage, showNext, screenShowNext, ditheredUrl, ditheredUrlMono, originalUrl, displayUrl, thumbnailUrl } from "./api.js";
import { $, $$, esc, toast, fmtBytes, fmtAgo, fmtUntil, frameColor } from "./ui.js";
import { openEditor } from "./editor.js";
import { openFramePreview } from "./frames.js";
import { galleryOrder } from "./gallery.js";

const gallery = $("#gallery");

// ── live lookups from the store (single source of truth) ──
const entryByName = (name) => (state.status?.upload_files || []).find((e) => e.name === name) || null;
const screens = () => state.status?.screens || {};
// friendly label for a frame: the server display name if set, else its provisioned
// (firmware) name. The provisioned name stays the internal key (data-frame, endpoints).
const frameLabel = (n) => screens()[n]?.display_name || n;
// a photo on an E1003 (mono) frame previews as the E1003 render, not the colour dither
const monoFrameOf = (name) => Object.values(screens()).find((s) => s.last_served === name && s.panel_type === "mono16_e1003") || null;
const onMonoFrame = (name) => !!monoFrameOf(name);
// portrait when the mono display shows content portrait — always the frame's orientation
// (every photo is composed upright for its frame)
const monoDisplayPortrait = (e) => {
  const fr = monoFrameOf(e.name); if (!fr) return false;
  return fr.orientation === "portrait";
};
const framePreviewUrl = (e) => (onMonoFrame(e.name) ? ditheredUrlMono(e, monoDisplayPortrait(e)) : ditheredUrl(e));
// is any E1003 (mono16) frame registered? gates the detail view's "Mono 16 Gray Panel" tab.
const hasMonoPanel = () => Object.values(screens()).some((s) => s.panel_type === "mono16_e1003");
// the E1003 mono render for the detail view (not tied to a frame): upright at the photo's
// own orientation. If the photo IS on a mono frame, follow that frame's display orientation.
const monoPreviewUrl = (e) =>
  ditheredUrlMono(e, onMonoFrame(e.name) ? monoDisplayPortrait(e) : e.effective_orientation === "portrait");
const arOf = (e) => (e.image_width && e.image_height ? e.image_width / e.image_height : 4 / 3);

// which frames are showing / have queued this image: the server's committed per-screen
// pick (pin or rotation) — the exact image the next serve returns.
const nextForScreen = (sc) => sc.next ?? sc.next_override ?? null;
const showingFrames = (name) => Object.keys(screens()).filter((s) => screens()[s].last_served === name);
const queuedFrames = (name) => Object.keys(screens()).filter((s) => nextForScreen(screens()[s]) === name);

// a portrait image sent to a landscape-filtering frame (and vice-versa) still serves
// — the server renders both orientations — but we warn before pinning it.
function mismatched(entry, sc) {
  if (!sc.filter_by_orientation) return false;
  // effective_orientation reflects a manual crop (a landscape source cropped to portrait
  // is portrait), matching the server's matches_orientation_filter + the detail view.
  const eff = entry.effective_orientation || entry.native_orientation;
  if (!eff || eff === "neutral") return false;   // square fits either panel
  return eff !== sc.orientation;
}

// ── shared action menu (used by the touch sheet, desktop menu, and detail panel) ──
const ICONS = {
  send:   '<path d="M5 12h14M13 6l6 6-6 6"/>',
  retry:  '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.5 4v4.5H16"/>',
  error:  '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4.5M12 17h.01"/>',
  details:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5h.01"/>',
  edit:   '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  trash:  '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
};
const ic = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${ICONS[n]}</svg>`;
const actBtn = (act, icon, label, cls) => `<button class="ctx-item ${cls || ""}" data-act="${act}">${ic(icon)}<span>${esc(label)}</span></button>`;

// Adaptive "send": 0–1 frame → global Show-next; 2+ frames → one chip per frame.
function menuItemsHTML(entry, inDetail) {
  let s = "";
  if (entry.status === "failed") {
    s += actBtn("retry", "retry", "Retry conversion");
    s += actBtn("error", "error", "View error", "warn");
  } else if (entry.status === "pending") {
    s += '<div class="ctx-note">Converting — hold on…</div>';
  } else {
    const names = Object.keys(screens());
    if (names.length >= 2) {
      s += '<div class="ctx-label">Send to a frame</div>';
      s += names.map((n) =>
        `<button class="ctx-item frame" data-act="send" data-frame="${esc(n)}"><span class="fdot" style="--fc:${frameColor(n)}"></span><span>${esc(frameLabel(n))}</span></button>`).join("");
    } else {
      s += actBtn("shownext", "send", "Show next");
    }
  }
  s += '<div class="ctx-sep"></div>';
  if (entry.status === "ok") s += actBtn("edit", "edit", "Edit photo…");
  if (entry.status === "ok" && !inDetail) s += actBtn("details", "details", "View details");
  s += actBtn("delete", "trash", "Delete", "del");
  return s;
}

// Returns true → dismiss the surface; "details" → open detail; false → stay open.
function runAction(btn, entry) {
  const act = btn.dataset.act;
  const span = btn.querySelector("span:not(.fdot)");   // frame chips have a .fdot dot span too — target the label
  if (act === "details") return "details";
  if (act === "edit") { openEditor(entry.name); return true; }
  if (act === "error") { toast(entry.error || "Conversion failed"); return false; }
  if (act === "retry") {
    retryImage(entry.name).then(() => { mutate(); toast(`Re-converting ${entry.name}…`); })
      .catch((e) => toast("Retry failed: " + e.message));
    return true;
  }
  if (act === "shownext") {
    showNext(entry.name).then(() => toast("Queued — shows on the next refresh"))
      .catch((e) => toast(e.status === 409 ? "Not ready yet — still converting" : "Show-next failed: " + e.message));
    return true;
  }
  if (act === "send") {
    const frame = btn.dataset.frame, sc = screens()[frame];
    // warn once (text-only swap, established two-step pattern) when orientation differs
    if (sc && mismatched(entry, sc) && !btn.classList.contains("armed")) {
      btn.classList.add("armed");
      if (span) span.textContent = `${frame} shows ${sc.orientation} — send anyway?`;
      return false;
    }
    screenShowNext(frame, entry.name).then(() => toast(`Sent to ${frame} — shows on its next refresh`))
      .catch((e) => toast(e.status === 409 ? "Not ready yet — still converting" : "Send failed: " + e.message));
    return true;
  }
  if (act === "delete") {
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      if (span) span.textContent = "Confirm delete?";
      return false;
    }
    deleteImage(entry.name).then(() => {
      mutate((st) => { st.upload_files = (st.upload_files || []).filter((e) => e.name !== entry.name); });
      toast(`${entry.name} deleted`);
    }).catch((e) => toast("Delete failed: " + e.message));
    return true;
  }
  return false;
}

// ── TOUCH: press-hold spotlight sheet (thumbnail + actions) ──
const ctxEl = $("#ctx");
const ctxSheet = $("#ctx-sheet");
function openSheet(entry) {
  closeMenu();
  const chip = entry.status === "pending" ? '<span class="mark c">Converting…</span>'
             : entry.status !== "ok"      ? '<span class="mark f">Couldn’t convert</span>' : "";
  ctxSheet.innerHTML =
    `<div class="ctx-preview"><img alt="${esc(entry.name)}">${chip}</div>` +
    `<div class="ctx-menu">${menuItemsHTML(entry, false)}</div>`;
  const pv = ctxSheet.querySelector("img");
  pv.addEventListener("error", () => { pv.style.visibility = "hidden"; });
  // ok → the frame render; not-yet-converted → the ALWAYS-SAFE server thumbnail (a JPEG that
  // decodes for every uploaded format), NEVER the raw original (a TIFF/HEIC won't paint here).
  pv.src = entry.status === "ok" ? framePreviewUrl(entry) : thumbnailUrl(entry);
  ctxEl._name = entry.name;
  ctxEl.hidden = false;
}
function closeSheet() { ctxEl.hidden = true; ctxEl._name = null; }
ctxEl.addEventListener("click", (e) => {
  if (e.target === ctxEl) { closeSheet(); return; }
  const btn = e.target.closest(".ctx-item"); if (!btn) return;
  const entry = entryByName(ctxEl._name); if (!entry) { closeSheet(); return; }
  const r = runAction(btn, entry);
  if (r === "details") { closeSheet(); openDetail(entry.name); }
  else if (r === true) closeSheet();
});

// ── DESKTOP: menu anchored over the tile, tile spotlit in place ──
const anchorMenu = $("#anchor-menu");
const anchorScrim = $("#anchor-scrim");
let menuTileEl = null;
function openMenu(entry, tileEl) {
  closeSheet(); closeMenu();
  anchorMenu.innerHTML = menuItemsHTML(entry, false);
  anchorMenu._name = entry.name;
  anchorScrim.hidden = false; anchorMenu.hidden = false;
  if (tileEl) { tileEl.classList.add("acting"); menuTileEl = tileEl; positionMenu(tileEl); }
}
function positionMenu(tileEl) {
  const r = tileEl.getBoundingClientRect();
  const mw = anchorMenu.offsetWidth, mh = anchorMenu.offsetHeight, m = 8;
  let left = r.left + (r.width - mw) / 2;
  let top = r.top + (r.height - mh) / 2;
  left = Math.max(m, Math.min(left, window.innerWidth - mw - m));
  top = Math.max(m, Math.min(top, window.innerHeight - mh - m));
  anchorMenu.style.left = left + "px"; anchorMenu.style.top = top + "px";
}
function closeMenu() {
  anchorMenu.hidden = true; anchorScrim.hidden = true; anchorMenu._name = null;
  if (menuTileEl) { menuTileEl.classList.remove("acting"); menuTileEl = null; }
}
anchorMenu.addEventListener("click", (e) => {
  const btn = e.target.closest(".ctx-item"); if (!btn) return;
  const entry = entryByName(anchorMenu._name); if (!entry) { closeMenu(); return; }
  const r = runAction(btn, entry);
  if (r === "details") { closeMenu(); openDetail(entry.name); }
  else if (r === true) closeMenu();
});
document.addEventListener("mousedown", (e) => {
  if (anchorMenu.hidden) return;
  if (e.target.closest("#anchor-menu") || e.target.closest(".tile-more")) return;
  closeMenu();
});
window.addEventListener("scroll", () => { if (!anchorMenu.hidden) closeMenu(); }, true);

// ── detail view (lightbox) ──
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

// client-side "zoom to fill": how much the image must scale past fit to fill the
// frame panel (cover ÷ contain). Compared to the server's crop_to_fill_threshold.
function zoomToFill(entry) {
  if (!entry.image_width || !entry.image_height) return null;
  const a = entry.image_width / entry.image_height;
  const panel = state.config?.panel;
  const land = panel && panel.visual_w && panel.visual_h ? panel.visual_w / panel.visual_h : 4 / 3;
  const p = entry.effective_orientation === "portrait" ? 1 / land : land;
  return Math.max(a / p, p / a) - 1;   // fraction of extra zoom needed
}

function ondisplayHTML(name) {
  const rows = (list, queued) => list.map((f) => {
    const sc = screens()[f];
    const t = queued
      ? (fmtUntil(sc.next_update_at) === "overdue" ? "overdue" : "in " + fmtUntil(sc.next_update_at))
      : fmtAgo(sc.last_seen);
    return `<div class="orow${queued ? " queued" : ""}" data-frame="${esc(f)}" data-kind="${queued ? "next" : "now"}" title="Preview this frame" style="--fc:${frameColor(f)}"><span class="d"></span><span class="fn">${esc(sc.display_name || f)}</span><span class="t">${esc(t)}</span></div>`;
  }).join("");
  const show = showingFrames(name), next = queuedFrames(name);
  return (show.length ? `<div class="ondisplay"><div class="oh">On display</div>${rows(show, false)}</div>` : "")
    + (next.length ? `<div class="ondisplay"><div class="oh">Up next</div>${rows(next, true)}</div>` : "");
}

function detailHTML(entry) {
  const ok = entry.status === "ok";
  const a = arOf(entry);
  const nat = entry.effective_orientation || entry.native_orientation || (a >= 1 ? "landscape" : "portrait");
  const unit = entry.dimension_unit || "px";
  const dims = entry.image_width && entry.image_height ? `${entry.image_width}×${entry.image_height} ${unit}` : "—";
  const orient = nat === "neutral" ? "Square" : cap(nat);
  const color = entry.is_bw == null ? "—" : entry.is_bw ? "Black &amp; White" : "6 color";
  const conv = entry.status === "pending" ? "converting…" : entry.last_conversion_seconds != null ? entry.last_conversion_seconds.toFixed(1) + " s" : "—";
  // show the name WITHOUT its extension (fits longer names without wrapping); the
  // extension moves to a "File type" field in the Technical tab
  const dotIdx = entry.name.lastIndexOf(".");
  const baseName = dotIdx > 0 ? entry.name.slice(0, dotIdx) : entry.name;
  const fileType = dotIdx > 0 ? entry.name.slice(dotIdx + 1).toUpperCase() : "—";
  const faces = entry.face_bboxes || [];
  const sd = state.status?.serve_data?.[entry.name];
  const lastShown = sd && sd.last_request ? fmtAgo(sd.last_request) : "Never";
  const shownCount = sd ? sd.total_show_count : 0;
  const z = zoomToFill(entry);
  const thr = state.config?.config?.crop_to_fill_threshold;
  const fill = z == null ? "—"
    : (thr != null && z <= thr) ? `${Math.round(z * 100)}% · auto-filled`
    : `needs ${Math.round(z * 100)}%${thr != null ? ` (limit ${Math.round(thr * 100)}%)` : ""}`;

  // Spectra 6 · Mono 16 (only when an E1003 is registered) · Original.
  // (Both panels are e-ink, so the view id is "spectra", not "eink".)
  const monoTab = hasMonoPanel() ? '<button data-view="mono">Mono 16</button>' : "";
  // no hard-coded active tab — openDetail/setDetailView set it from the photo's current frame.
  const toggle = ok
    ? '<div class="dtoggle"><button data-view="spectra">Spectra 6</button>' + monoTab + '<button data-view="orig">Original</button></div>'
    : "";
  // Non-ok photos have no e-ink render and no guaranteed browser-safe original yet → the media
  // shows the always-safe thumbnail plus this status chip (there is no view toggle for them).
  const chip = ok ? ""
    : entry.status === "pending" ? '<span class="mark c">Converting…</span>'
    : '<span class="mark f">Couldn’t convert</span>';
  // Download original — the RAW bytes as uploaded (/original, not /display). Detail-only, first in
  // the action row. A native <a download>; the click handler lets the browser handle it.
  const downloadHTML =
    `<a class="ctx-item" href="${originalUrl(entry)}" download="${esc(entry.name)}">` +
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>' +
      '<span>Download original</span>' +
    '</a>';
  const facePill = faces.length ? '<button class="face-pill" data-faces title="Show detected face boxes" hidden>Faces</button>' : "";
  const faceLayer = faces.length
    ? '<div class="face-layer" aria-hidden="true">' + faces.map((b) =>
        `<i style="left:${b[0] * 100}%;top:${b[1] * 100}%;width:${b[2] * 100}%;height:${b[3] * 100}%"></i>`).join("") + "</div>"
    : "";

  return (
    '<div class="detail-media">' +
      toggle + facePill + chip +
      '<div class="dstage">' +
        '<img id="detail-img" alt="' + esc(entry.name) + '" title="Open full size in a new tab">' +
        faceLayer +
      '</div>' +
      '<span class="zoomhint">↗ Click to open full size</span>' +
    '</div>' +
    '<div class="detail-side">' +
      '<h2>' + esc(baseName) + '</h2>' +
      ondisplayHTML(entry.name) +
      '<div class="dtabs" role="tablist">' +
        '<button data-tab="info" class="on" role="tab">Info</button>' +
        '<button data-tab="tech" role="tab">Technical</button>' +
      '</div>' +
      '<div class="dpanels">' +
        '<div class="dpanel on" data-panel="info"><div class="dmeta">' +
          `<span class="k">Dimensions</span><span class="v">${dims}</span>` +
          `<span class="k">Orientation</span><span class="v">${orient}</span>` +
          `<span class="k">Zoom to fill</span><span class="v">${fill}</span>` +
          `<span class="k">File size</span><span class="v">${fmtBytes(entry.size_bytes)}</span>` +
          `<span class="k">Color</span><span class="v">${color}</span>` +
          `<span class="k">Last displayed</span><span class="v">${lastShown}</span>` +
        '</div></div>' +
        '<div class="dpanel" data-panel="tech"><div class="dmeta">' +
          `<span class="k">File type</span><span class="v">${esc(fileType)}</span>` +
          `<span class="k">Shown</span><span class="v">${shownCount}&times;</span>` +
          `<span class="k">Convert time</span><span class="v">${conv}</span>` +
          `<span class="k">Faces</span><span class="v">${faces.length ? faces.length + " detected" : "None"}</span>` +
        '</div></div>' +
      '</div>' +
      '<div class="ctx-menu detail-actions">' + downloadHTML + menuItemsHTML(entry, true) + '</div>' +
    '</div>'
  );
}

const detailEl = $("#detail");
// the image URL for a detail view: "orig" | "mono" (E1003) | "spectra" (Spectra 6).
// Each tab shows exactly its labelled render REGARDLESS of which frame the photo is on:
// "spectra" is ALWAYS the colour Spectra dither, "mono" is ALWAYS the E1003 render. (Using
// framePreviewUrl here made the Spectra 6 tab show the MONO render for a photo on a mono
// frame — the tab is labelled Spectra 6, so it must be colour.)
function detailViewUrl(entry, view) {
  if (view === "orig") return displayUrl(entry);   // browser-safe rendition (NOT raw /original)
  if (view === "thumb") return thumbnailUrl(entry); // non-ok photos: the only always-decodable image
  if (view === "mono") return monoPreviewUrl(entry);
  return ditheredUrl(entry);   // spectra: always the colour render
}
function setDetailView(view) {
  // back-compat: a boolean still means "original or not".
  if (view === true) view = "orig";
  else if (view === false) view = "spectra";
  const entry = entryByName(detailEl._name); if (!entry) return;
  const img = detailEl.querySelector("#detail-img");
  img.classList.remove("broken");
  img.src = detailViewUrl(entry, view);
  detailEl._view = view;
  // keep the toggle buttons' active state in sync with the shown view (so a frame-based
  // default on open, not just clicks, highlights the right tab).
  detailEl.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  // face boxes are normalized to the ORIGINAL image → only meaningful on that view.
  // Auto-show them on Original (so they're discoverable — the pill toggles them off);
  // hide on the panel renders, where the cropped render would misplace them.
  const orig = view === "orig";
  const pill = detailEl.querySelector(".face-pill");
  if (pill) {
    pill.hidden = !orig;
    pill.classList.toggle("on", orig);
    detailEl.querySelector(".face-layer")?.classList.toggle("show", orig);
  }
}
function openDetail(name) {
  const entry = entryByName(name); if (!entry) return;
  closeSheet(); closeMenu();
  $("#detail-body").innerHTML = detailHTML(entry);
  detailEl._name = name;
  const img = detailEl.querySelector("#detail-img");
  img.addEventListener("error", () => img.classList.add("broken"));
  // Default the preview to the render type of the frame this photo is CURRENTLY on: a photo on
  // an E1003 mono frame opens on the Mono 16 tab, otherwise Spectra 6 (colour). Pending photos
  // have no e-ink render yet → Original. (A photo is only ever on one frame at a time.)
  // ok → the current frame's render (mono/spectra); non-ok has no e-ink render and no guaranteed
  // browser-safe original yet → the thumbnail (the status chip in the media conveys why). Non-ok
  // has no view toggle, so "orig"/"spectra" tabs can't be reached — no 404 broken square.
  const defaultView = entry.status !== "ok" ? "thumb" : (onMonoFrame(entry.name) ? "mono" : "spectra");
  setDetailView(defaultView);
  detailEl.hidden = false;
  updateDetailNav();
}
function closeDetail() { detailEl.hidden = true; detailEl._name = null; }

// Prev/next photo in the detail lightbox — shared by the ← / → keys and the flanking chevrons.
// step: -1 prev, +1 next. Walks galleryOrder (the exact on-screen order); clamps at the ends.
function navDetail(step) {
  if (detailEl.hidden || !detailEl._name) return;
  const order = galleryOrder();
  const i = order.indexOf(detailEl._name);
  if (i < 0) return;
  const j = i + step;
  if (j < 0 || j >= order.length) return;
  openDetail(order[j]);
}
// Keep the chevrons in sync with the current position: hide both when there's nothing to page
// through (0–1 photos), dim (disable) prev at the first photo and next at the last.
function updateDetailNav() {
  const prev = detailEl.querySelector(".detail-nav.prev");
  const next = detailEl.querySelector(".detail-nav.next");
  if (!prev || !next) return;
  const order = galleryOrder();
  const i = order.indexOf(detailEl._name);
  const pageable = i >= 0 && order.length > 1;
  prev.hidden = next.hidden = !pageable;
  prev.disabled = i <= 0;
  next.disabled = i < 0 || i >= order.length - 1;
}

detailEl.addEventListener("click", (e) => {
  if (e.target === detailEl || e.target.closest("[data-close]")) { closeDetail(); return; }
  // Prev/next chevrons (flank the card) → same step as the ← / → keys.
  const nav = e.target.closest("[data-nav]");
  if (nav) { navDetail(nav.dataset.nav === "prev" ? -1 : 1); return; }
  // On display / Up next row → frame-preview modal, stacked OVER this lightbox (detail
  // stays open behind it; dismissing the preview lands you back here).
  const orow = e.target.closest(".orow[data-frame]");
  if (orow) { openFramePreview(orow.dataset.frame, orow.dataset.kind); return; }
  const vb = e.target.closest("[data-view]");
  if (vb) {
    detailEl.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b === vb));
    setDetailView(vb.dataset.view);
    return;
  }
  const fb = e.target.closest("[data-faces]");
  if (fb) {
    const on = fb.classList.toggle("on");
    detailEl.querySelector(".face-layer")?.classList.toggle("show", on);
    return;
  }
  const tb = e.target.closest("[data-tab]");
  if (tb) {
    detailEl.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b === tb));
    detailEl.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("on", p.dataset.panel === tb.dataset.tab));
    return;
  }
  if (e.target.closest("#detail-img")) {
    const entry = entryByName(detailEl._name);
    if (entry) window.open(detailViewUrl(entry, detailEl._view), "_blank", "noopener");
    return;
  }
  const btn = e.target.closest(".ctx-item"); if (!btn) return;
  // "Download original" is a native <a download> — let the browser save the file; it's not a
  // runAction and must not close the lightbox out from under the download.
  if (btn.hasAttribute("download")) return;
  const entry = entryByName(detailEl._name); if (!entry) { closeDetail(); return; }
  if (runAction(btn, entry) === true) closeDetail();
});

// ── tile gestures (delegated on the gallery so they survive re-layout) ──
let lpTimer = null, suppressClick = false;
gallery.addEventListener("click", (e) => {
  const el = e.target.closest(".cell-tile"); if (!el) return;
  const name = el.dataset.name, entry = entryByName(name); if (!entry) return;
  if (e.target.closest(".tile-more")) {                       // desktop ⋯ — toggle
    e.stopPropagation();
    if (!anchorMenu.hidden && menuTileEl === el) closeMenu();
    else openMenu(entry, el);
    return;
  }
  if (suppressClick) { suppressClick = false; return; }        // was a long-press
  // frame badge → frame-preview modal, BOTH pointer types. Safe on touch because a plain
  // tile tap is a no-op on mobile (below): a missed badge tap opens nothing wrong.
  const badge = e.target.closest(".fbadge");
  if (badge && badge.dataset.frame) { openFramePreview(badge.dataset.frame, "now"); return; }
  // touch devices: a plain tap does nothing — press-hold opens the action sheet, and
  // its "View details" opens this modal. Tap-to-open-detail is desktop (hover) only.
  if (window.matchMedia && window.matchMedia("(hover: none)").matches) return;
  openDetail(name);
});
gallery.addEventListener("contextmenu", (e) => {
  const el = e.target.closest(".cell-tile"); if (!el) return;
  const entry = entryByName(el.dataset.name); if (!entry) return;
  e.preventDefault();
  if (window.matchMedia && window.matchMedia("(hover: none)").matches) return;   // touch uses the sheet
  openMenu(entry, el);
});
gallery.addEventListener("touchstart", (e) => {
  suppressClick = false;
  if (e.touches.length !== 1) { clearTimeout(lpTimer); return; }
  const el = e.target.closest(".cell-tile"); if (!el) return;
  const entry = entryByName(el.dataset.name); if (!entry) return;
  lpTimer = setTimeout(() => { suppressClick = true; openSheet(entry); }, 480);
}, { passive: true });
const cancelLP = () => clearTimeout(lpTimer);
gallery.addEventListener("touchmove", cancelLP, { passive: true });
gallery.addEventListener("touchend", cancelLP, { passive: true });

// Escape closes photo surfaces (drawers are handled in main.js). One layer per press:
// while the frame-preview modal is stacked on top, this Escape is ITS dismissal
// (frames.js closes it) — the detail lightbox underneath takes the NEXT press.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#frame-preview").hidden) return;
    closeDetail(); closeSheet(); closeMenu();
    return;
  }
  // Desktop: ← / → step to the prev/next photo while the detail lightbox is the TOP surface —
  // it walks the gallery in exactly the on-screen order (galleryOrder), so switching between
  // images is a keypress, not a close-click-reopen. Skipped when a frame-preview or the editor
  // is stacked over detail, when typing in a field, or with a modifier held (browser shortcuts).
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  if (detailEl.hidden || !detailEl._name) return;
  if (!$("#frame-preview").hidden || !$("#pedit-overlay").hidden) return;
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  e.preventDefault();
  navDetail(e.key === "ArrowLeft" ? -1 : 1);
});

// If the open photo is deleted out from under us (another client / poll), close detail.
subscribe((what) => {
  if (what === "status" && detailEl._name && !entryByName(detailEl._name)) closeDetail();
});
