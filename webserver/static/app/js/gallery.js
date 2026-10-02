// gallery.js — the photo grid. Read-only in M2: real thumbnails, true justified
// layout, per-frame "now showing" badges, converting markers, empty states, and a
// 5s poll that reuses tile DOM (never re-fetches or re-lays-out unless something
// actually changed) so polling never flickers. Actions land in M3.
//
// Ported from Plans/prototypes/app-mockup.html (build/justifyGrid/justifyAll/
// setFilter/tileEl) — the only semantic change is TILES → state.status.upload_files
// and the canvas paint → <img src=/thumbnail>.

import { state, subscribe } from "./state.js";
import { thumbnailUrl, ditheredUrl, ditheredUrlMono } from "./api.js";
import { $, $$, esc, frameColor, isMobileVp, fmtAgo } from "./ui.js";

const root = document.documentElement;
const gallery = $("#gallery");
const sizeCtrl = $("#size-ctrl");
const sizeSlider = $("#size-slider");

// filter → layout. Every view uses the adjustable "immersive" height (slider / pinch).
// ("Grouped" — one Landscape section then one Portrait — was removed: on a large library you
//  scroll forever to reach the second section, so it stopped being a way to find anything.)
let filter = "mixed";
const FILTER_LAYOUT = { mixed: "immersive", landscape: "immersive", portrait: "immersive" };

// ── finding: sort + kind ──────────────────────────────────────────────────────
// Below this many photos the whole library is a screen or two of scrolling, so Sort and Show
// can only tell you things you can already see — they stay hidden rather than add clutter.
const FIND_MIN = 60;
let sortMode = "added_desc";
let kind = "all";

// Kind is a single choice, so these are lenses rather than buckets: a B&W photo with faces
// appears under both B&W and People, which is invisible because only one can be active.
// is_bw is tri-state (null = the classifier never ran / is disabled) and must not read as false
// in a way that hides photos: an unknown photo is simply not B&W-classified.
const faceCount = (e) => (e.face_bboxes || []).length;
const KINDS = {
  all: () => true,
  bw: (e) => e.is_bw === true,
  people: (e) => faceCount(e) > 0,
  nopeople: (e) => faceCount(e) === 0,
};

// last_request is the only rotation field with real variance — the scheduler works to
// EQUALISE show counts (least-shown-first), so sorting by count degenerates into its
// tie-break. Never-served sorts to the extreme of both directions, which is where it belongs.
// /status sends last_request as an ISO STRING (flask_app: isoformat), not epoch seconds —
// parse it the same way the rest of the app does (ui.fmtAgo) or every comparison is NaN.
const lastShownISO = (e) => state.status?.serve_data?.[e.name]?.last_request || null;
function lastShown(e) {
  const iso = lastShownISO(e);
  if (!iso) return 0;                       // never served → sorts to the extreme
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? t / 1000 : 0;
}
const SORTS = {
  added_desc: (a, b) => (b.added_at || 0) - (a.added_at || 0),
  added_asc: (a, b) => (a.added_at || 0) - (b.added_at || 0),
  name_asc: (a, b) => a.name.localeCompare(b.name),
  name_desc: (a, b) => b.name.localeCompare(a.name),
  unseen: (a, b) => lastShown(a) - lastShown(b),
  recent: (a, b) => lastShown(b) - lastShown(a),
};
const isTimeSort = () => sortMode === "unseen" || sortMode === "recent";

// Time buckets label a last-shown ordering so it reads as an answer rather than an
// unexplained reordering. Only used when a time sort is active.
const DAY = 86400;
const TIME_BUCKETS = [
  ["Never shown", (s) => !s],
  ["Over a year", (s, now) => now - s > 365 * DAY],
  ["6–12 months", (s, now) => now - s > 182 * DAY],
  ["1–6 months", (s, now) => now - s > 30 * DAY],
  ["This month", () => true],
];
let immersiveH = +sizeSlider.value || 320;   // adjustable in immersive layout only
// slider/pinch bounds — SMAX also flips the gallery to one-tile-per-row at max zoom
const SMIN = +sizeSlider.min || 150, SMAX = +sizeSlider.max || 600;

// keyed tile cache: name → element (survives polls so images aren't re-fetched)
const tileCache = new Map();
let lastStructural = "";   // gate: only rebuild DOM + re-justify when structure changes
let visibleOrder = [];     // flat top-to-bottom order of the grid, for detail-view ←/→ nav

// The names currently in the grid, in exactly the order they're rendered (respects the active
// filter: mixed = the active sort order; landscape/portrait = that half only).
// Kept in sync by updateGallery below, so the detail-view arrow keys (photo.js) step through
// precisely what's on screen.
export const galleryOrder = () => visibleOrder;

// {shown, total} for the header count. main.js owns #meta and renders "N of M photos" from
// this; gallery fires "gallery:find" when the user changes sort/kind/shape so the count
// updates immediately instead of waiting for the next 5s poll.
let counts = { shown: 0, total: 0 };
export const galleryCounts = () => counts;

// ── per-entry derivations from the live status payload ──
// which connected frames are currently displaying this image
function framesShowing(name) {
  const screens = state.status?.screens || {};
  return Object.keys(screens).filter((s) => screens[s].last_served === name);
}
// a photo currently ON a frame shows its DITHERED panel preview in the gallery (the true
// "on the wall" look) — so its tile takes the panel's shape rather than the photo's own.
function showsDithered(e) { return e.status === "ok" && framesShowing(e.name).length > 0; }
// the mono (E1003) frame this photo is on, if any → its config (for orientation).
function monoFrameOf(name) {
  const screens = state.status?.screens || {};
  const s = framesShowing(name).find((n) => screens[n]?.panel_type === "mono16_e1003");
  return s ? screens[s] : null;
}
function onMonoFrame(name) { return !!monoFrameOf(name); }
// The frame a photo's tile mirrors: its mono frame if it's on one (the mono render is the
// distinctive one), else the first frame showing it. A photo is normally on one frame — the
// scheduler avoids duplicates — so "first" only arbitrates the rare manual-pin case.
function tileFrameOf(e) {
  const screens = state.status?.screens || {};
  return monoFrameOf(e.name) || screens[framesShowing(e.name)[0]] || null;
}
// The orientation this photo is DISPLAYED in: the showing frame's own, because every photo
// is composed upright for its frame. Off-frame, the photo's own effective orientation.
// (This is what the tile must request — asking without an orientation returned the photo's
// framing while the glass showed the frame's, so the tile contradicted the wall.)
function displayPortrait(e) {
  const fr = tileFrameOf(e);
  if (fr) return fr.orientation === "portrait";
  return e.effective_orientation === "portrait";
}
const framePreviewUrl = (e) => {
  const portrait = displayPortrait(e);
  return onMonoFrame(e.name)
    ? ditheredUrlMono(e, portrait)
    : ditheredUrl(e, portrait ? "portrait" : "landscape");
};
function panelAr(e) {
  const p = state.config?.panel;
  const w = (p && p.visual_w) || 1600, h = (p && p.visual_h) || 1200;
  // the tile takes the shape of what's actually displayed (the frame's orientation when
  // on a frame), so tile shape, render, and glass all agree.
  return displayPortrait(e) ? h / w : w / h;
}
function arOf(e) {
  // on-frame (dithered thumb) OR any edited photo (its thumbnail is cropped to the panel) →
  // lay out at the panel aspect in the EFFECTIVE orientation; otherwise the true photo aspect
  if (showsDithered(e) || e.edited) return panelAr(e);
  return e.image_width && e.image_height ? e.image_width / e.image_height : 4 / 3;
}
function dimText(e) {
  // When you've ordered by last-shown, the caption reports THAT — otherwise the sort is an
  // invisible reordering you can't read or verify from the grid.
  if (isTimeSort() && e.status === "ok") {
    const iso = lastShownISO(e);
    return iso ? fmtAgo(iso) : "never shown";
  }
  if (e.image_width && e.image_height) return `${e.image_width}×${e.image_height}`;
  return e.status === "pending" ? "converting" : "—";
}
function badgesFor(name) {
  // label = server display name if set, else the provisioned frame name (the key)
  return framesShowing(name).map((s) => ({
    name: s, color: frameColor(s), label: state.status?.screens?.[s]?.display_name || s,
  }));
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
      `<span class="fbadge" data-frame="${esc(b.name)}" title="Preview this frame" style="--fc:${b.color}"><span class="d"></span>${esc(b.label)}</span>`).join("") + "</div>";
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
    + (badges.length ? ", showing on " + badges.map((b) => b.label).join(" and ") : "")
    + (conv ? ", converting" : "");

  // thumbnail: the DITHERED panel preview when this photo is on a frame (mirrors the
  // wall), else the normal thumbnail. Only (re)assign src when the cache-bust key
  // changes, so identical images are NOT re-fetched on every poll (no flicker).
  const url = showsDithered(entry) ? framePreviewUrl(entry) : thumbnailUrl(entry);
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

// Split a last-shown ordering into labelled sections, so "Longest unseen" reads as an answer
// ("Over a year: 12") instead of an unexplained reordering. Sections run oldest-first for
// "Longest unseen" and newest-first for "Recently shown", matching the sort's direction.
function timeSections(list) {
  const now = Date.now() / 1000;
  const left = list.slice();
  const out = [];
  for (const [label, test] of TIME_BUCKETS) {
    const entries = [];
    for (let i = left.length - 1; i >= 0; i--) {
      if (test(lastShown(left[i]), now)) entries.unshift(left.splice(i, 1)[0]);
    }
    if (entries.length) { entries.sort(SORTS[sortMode]); out.push({ label, entries }); }
  }
  return sortMode === "recent" ? out.reverse() : out;
}

// Sort/Show appear only once the library is big enough to need them (FIND_MIN). If it drops
// back under, any active state is cleared too — otherwise a filter could be left on with no
// visible control to turn it off.
function syncFindControls(total) {
  const show = total >= FIND_MIN;
  const sw = $("#sortwrap"), kt = $("#ktabs");
  if (sw) sw.hidden = !show;
  if (kt) kt.hidden = !show;
  if (!show) { kind = "all"; sortMode = "added_desc"; }
}

const FILTERED_EMPTY_HTML =
  '<div class="gempty">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M4 19l6-6 4 4 2.5-2.5L21 19"/></svg>' +
    '<b>No photos match</b>' +
    '<span>Nothing in your library fits this combination.</span>' +
    '<button class="ghost-btn" id="find-reset">Show all photos</button>' +
  '</div>';

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
  const ready = (st.upload_files || []).filter((e) => e.status !== "failed");

  // prune cache of names that no longer exist (deleted upstream) — keyed on the WHOLE
  // library, never the filtered view, or filtering would evict tiles it later needs.
  const live = new Set(ready.map((e) => e.name));
  for (const name of [...tileCache.keys()]) if (!live.has(name)) tileCache.delete(name);

  // kind filter, then sort. The default (newest added first) is the original behaviour, so
  // fresh uploads still land at the top of every view unless you asked for another order.
  syncFindControls(ready.length);   // before filtering, so a shrunken library can't strand a filter
  const all = ready.filter(KINDS[kind]).slice().sort(SORTS[sortMode]);
  counts = { shown: all.length, total: ready.length };

  const land = all.filter((e) => arOf(e) >= 1);
  const port = all.filter((e) => arOf(e) < 1);

  // Structure gate: the ordered (name:ar) list for the current filter fully
  // determines the layout. If it's unchanged, skip the rebuild + re-justify and
  // only refresh per-tile content (badges/markers/thumb) in place — no flicker.
  let groups;   // [{label|null, entries}]
  const shaped = filter === "landscape" ? land : filter === "portrait" ? port : all;
  if (!shaped.length) groups = null;
  else if (isTimeSort()) groups = timeSections(shaped);   // labelled by how long unseen
  else if (filter === "mixed") groups = [{ label: null, entries: shaped }];
  else groups = [{ label: filter === "landscape" ? "Landscape" : "Portrait", entries: shaped }];

  // Flatten the SAME `groups` that drives rendering below → the exact on-screen order, no drift.
  visibleOrder = groups ? groups.flatMap((g) => g.entries).map((e) => e.name) : [];

  const structural = [filter, sortMode, kind].join("|") + "|" + (groups ? groups.map((g) =>
    (g.label || "") + ":" + g.entries.map((e) => e.name + "@" + arOf(e).toFixed(4)).join(",")).join(";") : "empty");

  if (structural === lastStructural) {
    // same layout — just repaint tile content in place (cheap, no reflow)
    if (groups) for (const g of groups) for (const e of g.entries) tileFor(e);
    return;
  }
  lastStructural = structural;

  // Preserve scroll across the rebuild. Editing a photo flips it to Converting and can
  // change its aspect, so the structural signature changes and we wipe+rebuild the DOM —
  // which otherwise resets the page to the top and makes you "lose your place" on the photo
  // you just edited. Capture the window scroll now, restore it after layout settles.
  const scrollY = window.scrollY;

  // structure changed → rebuild the gallery frame (reusing cached tiles) + justify
  gallery.textContent = "";
  if (!groups) {
    // "no photos at all" and "nothing matches your filter" are different dead ends and only
    // the second one is recoverable — give it a way out.
    const filtered = ready.length > 0;
    gallery.innerHTML = filtered ? FILTERED_EMPTY_HTML : EMPTY_HTML;
    if (filtered) $("#find-reset")?.addEventListener("click", resetFinding);
    return;
  }
  if (groups.length === 1 && groups[0].label === null) gallery.appendChild(gridOf(groups[0].entries));
  else groups.forEach((g) => gallery.appendChild(section(g.label, g.entries)));
  justifyAll();

  // Restore now and again after justifyAll's height change lands (rAF), clamped to the new
  // document height so we never scroll past the end.
  const restore = () => {
    const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    window.scrollTo(0, Math.min(scrollY, max));
  };
  restore();
  requestAnimationFrame(restore);
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
  let row = [], sum = 0;
  const flush = (fill) => {
    const jrow = document.createElement("div");
    jrow.className = "jrow";
    jrow.style.gap = gap + "px";
    const h = fill ? (W - gap * (row.length - 1)) / sum : targetH;
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
function pressTabs(sel, attr, value) {
  $$(sel + " button").forEach((b) => {
    const on = b.dataset[attr] === value;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
}

// every finding change ends here: repaint, and tell main.js so the header count follows
// immediately rather than at the next 5s poll
function applyFinding() {
  lastStructural = "";   // force a rebuild — order and/or membership changed
  updateGallery();
  document.dispatchEvent(new CustomEvent("gallery:find"));
}

function setFilter(f) {
  filter = f;
  root.setAttribute("data-layout", FILTER_LAYOUT[f]);
  pressTabs("#ftabs", "filter", f);
  sizeCtrl.hidden = FILTER_LAYOUT[f] !== "immersive";
  applyFinding();
}

function setKind(k) {
  kind = k;
  pressTabs("#ktabs", "kind", k);
  applyFinding();
}

function setSort(s) {
  sortMode = s;
  const sel = $("#sortsel");
  if (sel && sel.value !== s) sel.value = s;
  applyFinding();
}

// the single way out of an over-narrow view (from the filtered empty state)
function resetFinding() {
  kind = "all"; sortMode = "added_desc"; filter = "mixed";
  pressTabs("#ktabs", "kind", "all");
  pressTabs("#ftabs", "filter", "mixed");
  const sel = $("#sortsel");
  if (sel) sel.value = "added_desc";
  root.setAttribute("data-layout", FILTER_LAYOUT.mixed);
  sizeCtrl.hidden = false;
  applyFinding();
}

$("#ftabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setFilter(b.dataset.filter);
});

$("#ktabs")?.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setKind(b.dataset.kind);
});

$("#sortsel")?.addEventListener("change", (e) => setSort(e.target.value));

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
