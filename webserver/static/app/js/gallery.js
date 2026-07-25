// gallery.js — the photo grid. Read-only in M2: real thumbnails, true justified
// layout, per-frame "now showing" badges, converting markers, empty states, and a
// 5s poll that reuses tile DOM (never re-fetches or re-lays-out unless something
// actually changed) so polling never flickers. Actions land in M3.
//
// Ported from Plans/prototypes/app-mockup.html (build/justifyGrid/justifyAll/
// setFilter/tileEl) — the only semantic change is TILES → state.status.upload_files
// and the canvas paint → <img src=/thumbnail>.

import { state, subscribe } from "./state.js";
import { thumbnailUrl, ditheredUrl } from "./api.js";
import { $, $$, esc, frameColor, isMobileVp } from "./ui.js";

const root = document.documentElement;
const gallery = $("#gallery");
const sizeCtrl = $("#size-ctrl");
const sizeSlider = $("#size-slider");

// filter → layout. Every view uses the adjustable "immersive" height (slider / pinch).
// Grouped additionally splits into Landscape/Portrait sections (see updateGallery),
// but shares the same size control as the rest.
let filter = "mixed";
const FILTER_LAYOUT = { mixed: "immersive", grouped: "immersive", landscape: "immersive", portrait: "immersive" };
let immersiveH = +sizeSlider.value || 320;   // adjustable in immersive layout only
// slider/pinch bounds — SMAX also flips the gallery to one-tile-per-row at max zoom
const SMIN = +sizeSlider.min || 150, SMAX = +sizeSlider.max || 600;

// keyed tile cache: name → element (survives polls so images aren't re-fetched)
const tileCache = new Map();
let lastStructural = "";   // gate: only rebuild DOM + re-justify when structure changes

// ── per-entry derivations from the live status payload ──
// which connected frames are currently displaying this image
function framesShowing(name) {
  const screens = state.status?.screens || {};
  return Object.keys(screens).filter((s) => screens[s].last_served === name);
}
// a photo currently ON a frame shows its DITHERED panel preview in the gallery (the true
// "on the wall" look) — so its tile takes the panel's shape rather than the photo's own.
function showsDithered(e) { return e.status === "ok" && framesShowing(e.name).length > 0; }
function panelAr(e) {
  const p = state.config?.panel;
  const w = (p && p.visual_w) || 1600, h = (p && p.visual_h) || 1200;
  return e.effective_orientation === "portrait" ? h / w : w / h;
}
function arOf(e) {
  // on-frame (dithered thumb) OR any edited photo (its thumbnail is cropped to the panel) →
  // lay out at the panel aspect in the EFFECTIVE orientation; otherwise the true photo aspect
  if (showsDithered(e) || e.edited) return panelAr(e);
  return e.image_width && e.image_height ? e.image_width / e.image_height : 4 / 3;
}
function dimText(e) {
  if (e.image_width && e.image_height) return `${e.image_width}×${e.image_height}`;
  return e.status === "pending" ? "converting" : "—";
}
function badgesFor(name) {
  return framesShowing(name).map((s) => ({ name: s, color: frameColor(s) }));
}

// ── one tile: built once, then updated in place by paintTile() ──
function makeTile(entry) {
  const el = document.createElement("div");
  el.className = "cell-tile";
  el.dataset.name = entry.name;
  el.innerHTML =
    '<span class="badges-slot"></span>' +
    '<button class="tile-more" aria-label="Photo actions" title="Actions"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></button>' +
    '<img alt="" loading="lazy">' +
    '<div class="cap" aria-hidden="true"><span class="nm"></span><span class="dt"></span></div>';
  const img = el.querySelector("img");
  img.addEventListener("error", () => img.classList.add("broken"));
  img.addEventListener("load", () => img.classList.remove("broken"));
  return el;
}

function paintTile(el, entry) {
  const conv = entry.status === "pending";
  el.classList.toggle("conv", conv);
  el.dataset.ar = arOf(entry);

  // badges (now-showing) or a converting marker
  const badges = badgesFor(entry.name);
  const slot = el.querySelector(".badges-slot");
  if (badges.length) {
    slot.innerHTML = '<div class="badges">' + badges.map((b) =>
      `<span class="fbadge" style="--fc:${b.color}"><span class="d"></span>${esc(b.name)}</span>`).join("") + "</div>";
  } else if (conv) {
    slot.innerHTML = '<span class="mark c">Converting</span>';
  } else {
    slot.innerHTML = "";
  }

  // caption
  el.querySelector(".cap .nm").textContent = entry.name;
  el.querySelector(".cap .dt").textContent = dimText(entry);

  // a11y on the image
  const img = el.querySelector("img");
  img.alt = entry.name
    + (badges.length ? ", showing on " + badges.map((b) => b.name).join(" and ") : "")
    + (conv ? ", converting" : "");

  // thumbnail: the DITHERED panel preview when this photo is on a frame (mirrors the
  // wall), else the normal thumbnail. Only (re)assign src when the cache-bust key
  // changes, so identical images are NOT re-fetched on every poll (no flicker).
  const url = showsDithered(entry) ? ditheredUrl(entry) : thumbnailUrl(entry);
  if (el.dataset.thumb !== url) { el.dataset.thumb = url; img.src = url; }
}

// get-or-create + paint a tile for an entry
function tileFor(entry) {
  let el = tileCache.get(entry.name);
  if (!el) { el = makeTile(entry); tileCache.set(entry.name, el); }
  paintTile(el, entry);
  return el;
}

// ── grid / section builders (tiles come from the cache) ──
function gridOf(entries) {
  const g = document.createElement("div");
  g.className = "grid";
  entries.forEach((e) => g.appendChild(tileFor(e)));
  return g;
}
function section(label, entries) {
  const s = document.createElement("section");
  s.className = "gsection";
  s.innerHTML = `<div class="ghead"><span class="glabel">${esc(label)}</span><span class="gcount">${entries.length}</span></div>`;
  if (entries.length) s.appendChild(gridOf(entries));
  else s.insertAdjacentHTML("beforeend", `<div class="gempty small">No ${esc(label.toLowerCase())} photos yet.</div>`);
  return s;
}

const EMPTY_HTML =
  '<div class="gempty">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M4 19l6-6 4 4 2.5-2.5L21 19"/></svg>' +
    '<b>No photos yet</b>' +
    '<span>Drag photos anywhere on this page, or press <u>Upload</u>. JPEG · HEIC · PNG · WEBP · AVIF · JXL</span>' +
  '</div>';

// ── the render entry point: called on every status poll ──
function updateGallery() {
  const st = state.status;
  if (!st) return;
  // failed photos live in the failed modal (M4), not the grid.
  // newest uploads first (by added_at) so fresh photos land at the top of every view;
  // stable sort keeps the backend's name order as the tie-break.
  const all = (st.upload_files || [])
    .filter((e) => e.status !== "failed")
    .sort((a, b) => (b.added_at || 0) - (a.added_at || 0));

  // prune cache of names that no longer exist (deleted upstream)
  const live = new Set(all.map((e) => e.name));
  for (const name of [...tileCache.keys()]) if (!live.has(name)) tileCache.delete(name);

  const land = all.filter((e) => arOf(e) >= 1);
  const port = all.filter((e) => arOf(e) < 1);

  // Structure gate: the ordered (name:ar) list for the current filter fully
  // determines the layout. If it's unchanged, skip the rebuild + re-justify and
  // only refresh per-tile content (badges/markers/thumb) in place — no flicker.
  let groups;   // [{label|null, entries}]
  if (!all.length) groups = null;
  else if (filter === "mixed") groups = [{ label: null, entries: all }];
  else if (filter === "landscape") groups = [{ label: "Landscape", entries: land }];
  else if (filter === "portrait") groups = [{ label: "Portrait", entries: port }];
  else {   // grouped: one Landscape section then one Portrait section (omit an empty half)
    groups = [];
    if (land.length) groups.push({ label: "Landscape", entries: land });
    if (port.length) groups.push({ label: "Portrait", entries: port });
  }

  const structural = filter + "|" + (groups ? groups.map((g) =>
    (g.label || "") + ":" + g.entries.map((e) => e.name + "@" + arOf(e).toFixed(4)).join(",")).join(";") : "empty");

  if (structural === lastStructural) {
    // same layout — just repaint tile content in place (cheap, no reflow)
    if (groups) for (const g of groups) for (const e of g.entries) tileFor(e);
    return;
  }
  lastStructural = structural;

  // structure changed → rebuild the gallery frame (reusing cached tiles) + justify
  gallery.textContent = "";
  if (!groups) { gallery.innerHTML = EMPTY_HTML; return; }
  if (filter === "mixed") gallery.appendChild(gridOf(groups[0].entries));   // no label, upload order
  else groups.forEach((g) => gallery.appendChild(section(g.label, g.entries)));
  justifyAll();
}

// ── true justified layout: fill each row by scaling the shared row height while
//    keeping every tile at its exact AR (never cropped). Ported verbatim. ──
//    singleColumn (max zoom): one tile per row so EVERY tile — portrait and
//    landscape alike — spans the full width, with matching widths.
function justifyGrid(grid, targetH, gap, singleColumn) {
  const W = grid.clientWidth;
  if (!W) return;
  const tiles = [...grid.querySelectorAll(".cell-tile")];   // survive previous rows
  grid.textContent = "";                                    // drop old .jrow wrappers
  let row = [], sum = 0, lastH = targetH;
  const flush = (fill) => {
    const jrow = document.createElement("div");
    jrow.className = "jrow";
    jrow.style.gap = gap + "px";
    const h = fill ? (W - gap * (row.length - 1)) / sum : lastH;
    if (fill) lastH = h;
    jrow.style.height = h + "px";
    row.forEach((el) => {
      const a = parseFloat(el.dataset.ar);
      el.style.flex = fill ? a + " 1 0" : "0 0 auto";
      el.style.width = fill ? "" : (a * h) + "px";
      el.style.height = "";
      jrow.appendChild(el);
    });
    grid.appendChild(jrow);
    row = []; sum = 0;
  };
  if (singleColumn) {
    // one tile per row: every tile spans the full width, height follows its AR.
    // Set width/height explicitly (not flex) so portraits fill exactly like
    // landscapes — a flex-grow row leaves the tile at ar×W, which is the bug.
    tiles.forEach((el) => {
      const a = parseFloat(el.dataset.ar);
      const jrow = document.createElement("div");
      jrow.className = "jrow";
      jrow.style.height = (W / a) + "px";     // full width ÷ aspect = the tile's height
      el.style.flex = "0 0 auto";
      el.style.width = W + "px";
      el.style.height = "";
      jrow.appendChild(el);
      grid.appendChild(jrow);
    });
    return;
  }
  tiles.forEach((el) => {
    const a = parseFloat(el.dataset.ar);
    row.push(el); sum += a;
    if (sum * targetH + gap * (row.length - 1) >= W) flush(true);
  });
  if (row.length) flush(false);
}
function justifyAll() {
  const immersive = root.getAttribute("data-layout") === "immersive";
  const h = immersive ? immersiveH : 250;
  const gap = 6;
  // Mobile only, at (or near) max zoom: switch to one-tile-per-row so portraits
  // fill the full width exactly like landscapes — their widths match. Desktop keeps
  // the justified grid at every zoom (never forces a single full-width column).
  const singleColumn = immersive && isMobileVp() && immersiveH >= SMAX - 1;
  root.style.setProperty("--gap", gap + "px");
  $$("#gallery .grid").forEach((g) => justifyGrid(g, h, gap, singleColumn));
}

// ── controls: filter tabs, size slider, pinch-zoom, resize ──
function setFilter(f) {
  filter = f;
  root.setAttribute("data-layout", FILTER_LAYOUT[f]);
  $$("#ftabs button").forEach((b) => b.classList.toggle("on", b.dataset.filter === f));
  sizeCtrl.hidden = FILTER_LAYOUT[f] !== "immersive";
  lastStructural = "";   // force a rebuild for the new arrangement
  updateGallery();
}

$("#ftabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setFilter(b.dataset.filter);
});

sizeSlider.addEventListener("input", (e) => { immersiveH = +e.target.value; justifyAll(); });

// mobile pinch-to-zoom (immersive only), clamped to the slider's range (SMIN/SMAX above)
let pinchD0 = 0, pinchH0 = 0;
const pdist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
gallery.addEventListener("touchstart", (e) => {
  if (e.touches.length === 2 && root.getAttribute("data-layout") === "immersive") {
    pinchD0 = pdist(e.touches); pinchH0 = immersiveH;
  }
}, { passive: true });
gallery.addEventListener("touchmove", (e) => {
  if (e.touches.length === 2 && pinchD0 && root.getAttribute("data-layout") === "immersive") {
    immersiveH = Math.max(SMIN, Math.min(SMAX, pinchH0 * pdist(e.touches) / pinchD0));
    sizeSlider.value = Math.round(immersiveH);
    justifyAll();
    e.preventDefault();
  }
}, { passive: false });
gallery.addEventListener("touchend", () => { pinchD0 = 0; });

let _rz;
window.addEventListener("resize", () => { clearTimeout(_rz); _rz = setTimeout(justifyAll, 120); });

// tile gestures (tap → detail, ⋯/right-click/press-hold → actions) live in photo.js

// ── boot: set initial layout, then track the store ──
root.setAttribute("data-layout", FILTER_LAYOUT[filter]);
sizeCtrl.hidden = FILTER_LAYOUT[filter] !== "immersive";
subscribe((what) => { if (what === "status") updateGallery(); });
if (state.status) updateGallery();
