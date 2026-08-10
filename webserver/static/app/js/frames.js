// frames.js — the connected-frame cards (in the Frames drawer), the ⋯ menu, the
// diagnostics modal, remove-frame, per-frame orientation/match-orientation PATCH,
// and cancelling a per-frame pin. Ported from the mockup's renderFrames / frame-menu
// / openFrameDiag; the demo FRAMES array becomes state.status.screens, the canvas
// thumbnails become <img src=/thumbnail>, and every control hits the real backend.

import { state, subscribe, mutate } from "./state.js";
import { deleteScreen, patchScreen, clearScreenShowNext, skipScreenNext, thumbnailUrl, ditheredUrl, ditheredUrlMono } from "./api.js";
import { $, $$, esc, toast, fmtAgo, fmtUntil, fmtUptime, frameColor, armConfirm, disarm } from "./ui.js";
import { framingChipHTML } from "./framing.js";

const fcards = $("#fcards");
const OVERDUE_GRACE_S = 120;   // wake jitter + clock drift allowance before "overdue"

const screens = () => state.status?.screens || {};
const entryByName = (name) => (state.status?.upload_files || []).find((e) => e.name === name) || null;

// The Frames drawer card shows what the frame shows: the render for that frame's
// orientation. Every photo is composed upright for its frame, so both panel types return
// an image already SHAPED to the frame (colour 1200x1600 / 1600x1200, mono 1404x1872 /
// 1872x1404) and the card just drops it in a frame-shaped box — same rule for both, no
// per-panel rotation. Identical to what the preview modal and the gallery tile request.
const isMonoFrame = (sc) => sc?.panel_type === "mono16_e1003";
function frameCardUrl(sc, entry) {
  if (!entry) return "";
  const framePortrait = sc.orientation === "portrait";
  if (isMonoFrame(sc)) return ditheredUrlMono(entry, framePortrait);
  return ditheredUrl(entry, framePortrait ? "portrait" : "landscape");
}
// the frame's next image: the server's committed per-screen pick (pin or rotation),
// i.e. the EXACT image the next serve returns — so "up next" always matches the frame.
const nextForScreen = (sc) => sc.next ?? sc.next_override ?? null;
function overdueSeconds(sc) {
  if (!sc.next_update_at) return 0;
  const by = (Date.now() - new Date(sc.next_update_at).getTime()) / 1000;
  return by > OVERDUE_GRACE_S ? by : 0;
}
function overdueText(s) {
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${Math.max(1, m)}m`;
}
const setImg = (img, url) => { if (img.dataset.src !== url) { img.dataset.src = url; if (url) { img.classList.remove("broken"); img.src = url; } else img.removeAttribute("src"); } };

// ── keyed frame cards (reused across polls; thumbnails only reload when they change) ──
const cardCache = new Map();
function makeCard(name) {
  const el = document.createElement("div");
  el.className = "fcard";
  el.dataset.frame = name;
  el.innerHTML =
    '<button class="fmore" data-fmore aria-label="Frame actions" title="Actions"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></button>' +
    '<div class="qstrip"><div class="fthumb">' +
      '<img class="c-now" alt="">' +
      '<img class="c-next" alt="" aria-hidden="true">' +
      '<button class="upnext-cancel" data-cancel aria-label="Cancel pinned image" title="Cancel pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '<span class="upnext">Up next</span>' +
      '<span class="fover" hidden></span>' +
    '</div></div>' +
    '<div class="info">' +
      '<span class="nm"><span class="d"></span><span class="nm-text"></span></span>' +
      '<span class="row idrow"><span class="ip"></span><span>fw <b class="fw"></b></span></span>' +
      '<span class="row"><span class="bt"><span class="cell"><i></i></span><span class="batt-pct"></span></span>' +
        '<span class="next-wrap">next <b class="next"></b></span><span>seen <b class="seen"></b></span></span>' +
    '</div>';
  el.querySelector(".c-now").addEventListener("error", function () { this.classList.add("broken"); });
  el.querySelector(".c-next").addEventListener("error", function () { this.classList.add("broken"); });
  return el;
}
function updateCard(el, name, sc) {
  el.style.setProperty("--fc", frameColor(name));

  const nowE = entryByName(sc.last_served);
  const upName = nextForScreen(sc), upE = entryByName(upName);

  // Card box = the FRAME's orientation, and both panel types return an image already shaped
  // to it — so the image fills the box directly, for colour and mono alike.
  const thumb = el.querySelector(".fthumb");
  const boxPortrait = sc.orientation === "portrait";
  thumb.className = "fthumb shape-" + (boxPortrait ? "portrait" : "landscape");

  const applyImg = (img, entry) => setImg(img, entry ? frameCardUrl(sc, entry) : "");
  applyImg(el.querySelector(".c-now"), nowE);
  applyImg(el.querySelector(".c-next"), upE);
  const pinned = !!sc.next_override;
  thumb.classList.toggle("pinned", pinned);
  el.querySelector(".upnext").textContent = pinned ? "Pinned" : "Up next";

  const od = overdueSeconds(sc);
  el.classList.toggle("overdue", od > 0);
  const fover = el.querySelector(".fover");
  fover.hidden = !(od > 0);
  if (od > 0) fover.textContent = `⚠ Overdue · ${overdueText(od)}`;

  const nmText = el.querySelector(".nm-text");
  nmText.textContent = sc.display_name || name;   // friendly name if set, else the provisioned name
  nmText.title = sc.display_name ? `Provisioned name: ${name}` : "";
  el.querySelector(".ip").textContent = sc.ip || "—";
  el.querySelector(".fw").textContent = sc.state?.fw || "—";
  const pct = sc.battery_percent;
  el.querySelector(".cell i").style.width = (pct ?? 0) + "%";
  el.querySelector(".batt-pct").textContent = pct != null ? pct + "%" : "—";
  const nextWrap = el.querySelector(".next-wrap");
  nextWrap.hidden = od > 0;   // the amber banner already says "overdue"
  el.querySelector(".next").textContent = fmtUntil(sc.next_update_at);
  el.querySelector(".seen").textContent = sc.last_seen ? fmtAgo(sc.last_seen).replace(" ago", "") : "—";
}
function renderFrames() {
  const names = Object.keys(screens());
  for (const n of [...cardCache.keys()]) if (!names.includes(n)) { cardCache.get(n).remove(); cardCache.delete(n); }
  if (!names.length) {
    if (!fcards.querySelector(".fempty")) fcards.innerHTML = '<div class="fempty">No frames connected yet — a frame appears here the first time it wakes up and checks in with this server.</div>';
    cardCache.clear();
    return;
  }
  if (fcards.querySelector(".fempty")) fcards.innerHTML = "";
  names.forEach((name, i) => {
    let el = cardCache.get(name);
    if (!el) { el = makeCard(name); cardCache.set(name, el); }
    updateCard(el, name, screens()[name]);
    if (fcards.children[i] !== el) fcards.insertBefore(el, fcards.children[i] || null);
  });
}

// ── ⋯ frame menu (View diagnostics / Remove frame) ──
const frameMenu = $("#frame-menu");
const frameScrim = $("#frame-scrim");
let menuFrame = null;
function openFrameMenu(btn, name) {
  menuFrame = name;
  frameMenu.innerHTML =
    '<button class="ctx-item" data-fact="preview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg><span>Preview Frame</span></button>' +
    '<button class="ctx-item" data-fact="rename"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17z"/><path d="M13.5 6.5l3 3"/></svg><span>Rename</span></button>' +
    '<button class="ctx-item" data-fact="skip"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 5v14l9-7z"/><path d="M18 5v14"/></svg><span>Skip up next</span></button>' +
    '<button class="ctx-item" data-fact="diag"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg><span>View diagnostics</span></button>' +
    '<button class="ctx-item del" data-fact="remove"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg><span class="lbl">Remove frame</span></button>';
  frameMenu.hidden = false; frameScrim.hidden = false;
  const r = btn.getBoundingClientRect(), mw = frameMenu.offsetWidth || 190, mh = frameMenu.offsetHeight || 96;
  let left = Math.max(8, r.right - mw), top = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
  frameMenu.style.left = left + "px"; frameMenu.style.top = top + "px";
}
function closeFrameMenu() { frameMenu.hidden = true; frameScrim.hidden = true; menuFrame = null; disarm(frameMenu.querySelector(".armed")); }
frameScrim.addEventListener("click", closeFrameMenu);
frameMenu.addEventListener("click", (e) => {
  const b = e.target.closest("[data-fact]"); if (!b) return;
  if (b.dataset.fact === "preview") { const n = menuFrame; closeFrameMenu(); openFramePreview(n, "now"); }
  else if (b.dataset.fact === "rename") { const n = menuFrame; closeFrameMenu(); openRename(n); }
  else if (b.dataset.fact === "skip") {
    const n = menuFrame; closeFrameMenu();
    skipScreenNext(n)
      .then((resp) => { mutate((st) => { if (st.screens[n]) st.screens[n].next = resp.next_image; }); toast(`Skipped — up next changed on ${n}`); })
      .catch((err) => toast("Skip failed: " + err.message));
  }
  else if (b.dataset.fact === "diag") { const n = menuFrame; closeFrameMenu(); openFrameDiag(n); }
  else if (b.dataset.fact === "remove") armConfirm(b, () => { const n = menuFrame; closeFrameMenu(); removeFrame(n); }, "Confirm remove?");
});

fcards.addEventListener("click", (e) => {
  const more = e.target.closest("[data-fmore]");
  if (more) { e.stopPropagation(); openFrameMenu(more, more.closest(".fcard").dataset.frame); return; }
  const cancel = e.target.closest("[data-cancel]");
  if (cancel) {
    e.stopPropagation();
    const name = cancel.closest(".fcard").dataset.frame;
    clearScreenShowNext(name).then(() => { mutate((st) => { if (st.screens[name]) st.screens[name].next_override = null; }); toast(`Unpinned ${name}`); })
      .catch((err) => toast("Cancel failed: " + err.message));
    return;
  }
  // tap the thumbnail → open the read-only frame preview (opens on "now"; toggle switches to up next)
  const thumb = e.target.closest(".fthumb");
  if (thumb) openFramePreview(thumb.closest(".fcard").dataset.frame, "now");
});

// ── diagnostics modal ──
const frameModal = $("#frame-modal");
let diagFrame = null;
const fstat = (k, v, cls) => `<div class="fstat"><span class="k">${esc(k)}</span><span class="v${cls ? " " + cls : ""}">${v}</span></div>`;
function openFrameDiag(name) {
  const sc = screens()[name]; if (!sc) return;
  diagFrame = name;
  const s = sc.state || {};
  $("#fdiag-name").textContent = name;
  $("#fdiag-ip").textContent = sc.ip || "";
  $("#fdiag-dot").style.background = frameColor(name);
  frameModal.querySelector(".fdiag-card").style.setProperty("--fc", frameColor(name));

  const pct = sc.battery_percent, mv = sc.battery_mv ?? s.bat_mv;
  const rssi = s.rssi, drift = s.clk_drift_s, sleepErr = s.sleep_err_s;
  const rssiCls = rssi != null && rssi <= -75 ? "bad" : rssi != null && rssi <= -67 ? "warn" : "";
  const battCls = pct != null && pct <= 20 ? "bad" : pct != null && pct <= 45 ? "warn" : "";
  const driftCls = drift != null && Math.abs(drift) >= 3 ? "warn" : "";
  const od = overdueSeconds(sc);
  const nextWake = od > 0 ? `${overdueText(od)} overdue` : fmtUntil(sc.next_update_at);
  const num = (v, unit) => (v == null ? "—" : `${v}${unit || ""}`);

  $("#fdiag-body").innerHTML =
    '<div class="fdiag-grid">' +
      fstat("Firmware", esc(s.fw ?? "—")) +
      fstat("Wake reason", esc(s.wake ?? "—")) +
      fstat("Uptime", fmtUptime(s.uptime_s)) +
      fstat("Battery", mv != null ? `${(mv / 1000).toFixed(2)} V · ${pct ?? "—"}%` : `${pct ?? "—"}%`, battCls) +
      fstat("Wi-Fi", num(rssi, " dBm"), rssiCls) +
      fstat("Free heap", num(s.heap_kb, " KB")) +
      fstat("Clock drift", num(drift, " s"), driftCls) +
      fstat("Sleep error", num(sleepErr, " s")) +
      fstat("Next wake", nextWake, od > 0 ? "bad" : "") +
      fstat("Regime", esc(s.regime ?? "—")) +
      fstat("USB", esc(s.usb ?? "—")) +
      fstat("Boot", esc(String(s.boot ?? "—"))) +
    "</div>" +
    '<div class="fdiag-opt"><div class="fo-txt"><span class="fo-t">Orientation</span>' +
      '<span class="fo-d">Which way this frame is mounted</span></div>' +
      '<div class="seg" id="fdiag-orient">' +
        `<button data-orient="landscape"${sc.orientation === "landscape" ? ' class="on"' : ""}>Landscape</button>` +
        `<button data-orient="portrait"${sc.orientation === "portrait" ? ' class="on"' : ""}>Portrait</button>` +
      "</div></div>" +
    '<div class="fdiag-opt"><div class="fo-txt"><span class="fo-t">Match orientation</span>' +
      "<span class=\"fo-d\">Only queue photos matching this frame's orientation</span></div>" +
      `<button class="sw${sc.filter_by_orientation ? " on" : ""}" id="fdiag-match" role="switch" aria-checked="${!!sc.filter_by_orientation}"><span></span></button></div>` +
    `<div><div class="flog-lab">Firmware log</div><div class="flog">${esc(sc.last_log || "No recent log.")}</div></div>`;

  $("#fdiag-orient").addEventListener("click", (e) => {
    const b = e.target.closest("[data-orient]"); if (!b || b.classList.contains("on")) return;
    const val = b.dataset.orient;
    $$("#fdiag-orient button").forEach((x) => x.classList.toggle("on", x === b));   // optimistic
    patchScreen(name, { orientation: val }).then(() => { mutate((st) => { if (st.screens[name]) st.screens[name].orientation = val; }); toast(`${name} → ${val}`); })
      .catch((err) => { toast("Update failed: " + err.message); openFrameDiag(name); });
  });
  const mt = $("#fdiag-match");
  mt.addEventListener("click", () => {
    const val = !mt.classList.contains("on");
    mt.classList.toggle("on", val); mt.setAttribute("aria-checked", String(val));   // optimistic
    patchScreen(name, { filter_by_orientation: val }).then(() => mutate((st) => { if (st.screens[name]) st.screens[name].filter_by_orientation = val; }))
      .catch((err) => { toast("Update failed: " + err.message); openFrameDiag(name); });
  });

  frameModal.hidden = false;
}
function closeFrameDiag() { frameModal.hidden = true; diagFrame = null; disarm($("#fdiag-remove")); }
frameModal.addEventListener("click", (e) => {
  if (e.target === frameModal || e.target.closest("[data-fdiag-close]")) closeFrameDiag();
});
$("#fdiag-remove").addEventListener("click", (e) => { if (diagFrame) armConfirm(e.currentTarget, () => { const n = diagFrame; removeFrame(n); }, "Confirm remove?"); });

function removeFrame(name) {
  deleteScreen(name).then(() => {
    closeFrameDiag(); closeFrameMenu();
    mutate((st) => { if (st.screens) delete st.screens[name]; });
    toast(`Removed “${name}” — it returns on its next check-in`);
  }).catch((err) => toast("Remove failed: " + err.message));
}

// ── rename dialog (sets a server-side display name; the provisioned name stays the internal key) ──
const renameModal = $("#rename-modal");
const rnInput = $("#rn-input");
let renameFrame = null;
function openRename(name) {
  const sc = screens()[name]; if (!sc) return;
  renameFrame = name;
  $("#rn-dot").style.background = frameColor(name);
  $("#rn-prov").textContent = name;
  rnInput.value = sc.display_name || name;
  renameModal.hidden = false;
  setTimeout(() => { rnInput.focus(); rnInput.select(); }, 30);
}
function closeRename() { renameModal.hidden = true; renameFrame = null; }
function commitRename(raw) {
  const name = renameFrame; if (!name) return;
  const dn = (raw || "").trim();
  // empty, or identical to the provisioned name => clear the override (reset)
  const send = (dn === "" || dn === name) ? "" : dn;
  patchScreen(name, { display_name: send })
    .then(() => {
      mutate((st) => { if (st.screens[name]) st.screens[name].display_name = send || null; });
      toast(send ? `Renamed to “${send}”` : `Reset to ${name}`);
      closeRename();
    })
    .catch((err) => toast("Rename failed: " + err.message));
}
$("#rn-save").addEventListener("click", () => commitRename(rnInput.value));
$("#rn-reset").addEventListener("click", () => commitRename(""));   // "" resets to provisioned
rnInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commitRename(rnInput.value); } });
renameModal.addEventListener("click", (e) => { if (e.target === renameModal || e.target.closest("[data-rn-close]")) closeRename(); });

// ── frame preview modal (read-only "as shown on this frame": now ↔ up next) ──
// A focused lightbox that mirrors what the panel is displaying — the same reframed,
// dither/mono render the wall shows, big enough to judge the crop and faces. The tile
// only opens it; the Now/Up-next swap is a toggle INSIDE the modal (identical on
// desktop and mobile, unlike the tile's hover-peek). Skip appears only on up-next.
const fpv = $("#frame-preview");
const fpvCard = fpv.querySelector(".fpv-card");
const fpvDevice = $("#fpv-device");
const fpvImg = $("#fpv-img");
const fpvEmpty = $("#fpv-empty");
const fpvName = $("#fpv-nametext");
const fpvDot = $("#fpv-dot");
const fpvMeta = $("#fpv-meta");
const fpvSkip = $("#fpv-skip");
const fpvLib = $("#fpv-lib");
let pvFrame = null, pvKind = "now";   // which frame, and which slot (now|next) is shown

// the image entry a given slot resolves to (upnext = the committed serve pick; now = last served)
const fpvEntry = (sc, kind) => entryByName(kind === "next" ? nextForScreen(sc) : sc.last_served);
// what THIS frame shows for `entry`, plus the box orientation — the same request the
// drawer card and the gallery tile make, so all three agree by construction.
function fpvRender(sc, entry) {
  const p = sc.orientation === "portrait";   // content always follows the frame
  if (!entry) return { url: "", portrait: p };
  return { url: frameCardUrl(sc, entry), portrait: p };
}
function renderFramePreview() {
  const sc = screens()[pvFrame];
  if (!sc) { closeFramePreview(); return; }   // frame removed while open → dismiss
  const color = frameColor(pvFrame);
  fpvCard.style.setProperty("--fc", color);
  fpvDot.style.background = color;
  fpvName.textContent = sc.display_name || pvFrame;

  fpv.querySelectorAll(".fpv-seg").forEach((b) => {
    const on = b.dataset.kind === pvKind;
    b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on));
  });

  const entry = fpvEntry(sc, pvKind);
  const { url, portrait } = fpvRender(sc, entry);
  fpvDevice.className = "fpv-device " + (portrait ? "portrait" : "landscape");
  fpvCard.classList.toggle("port", portrait);
  fpvCard.classList.toggle("land", !portrait);

  if (url) {
    fpvEmpty.hidden = true;
    if (fpvImg.dataset.src !== url) { fpvImg.dataset.src = url; fpvImg.classList.remove("broken"); fpvImg.src = url; }
  } else {
    fpvImg.dataset.src = ""; fpvImg.removeAttribute("src"); fpvImg.classList.add("broken");
    fpvEmpty.hidden = false;
    fpvEmpty.textContent = pvKind === "next" ? "No image queued yet" : "Nothing shown yet";
  }

  const panel = isMonoFrame(sc) ? "E1003 · mono" : "Spectra 6";
  // The last chip used to repeat the FRAME's orientation — a word the device drawing already
  // gives you. It now reports what the eye can't: whether anything was cropped away, and for a
  // letterboxed photo the slider value that would fill it. A glyph at the photo's true
  // proportions leads it when the photo's own shape disagrees with the frame.
  const fit = entry
    ? framingChipHTML(entry, portrait, isMonoFrame(sc), state.config?.config?.crop_to_fill_threshold)
    : "";
  fpvMeta.innerHTML =
    (entry ? `<span class="fpv-chip">${esc(entry.name)}</span><span class="fpv-sep">·</span>` : "") +
    `<span class="fpv-chip panel">${esc(panel)}</span>` +
    (fit ? `<span class="fpv-sep">·</span>${fit}` : "");

  fpvSkip.hidden = !(pvKind === "next" && entry);   // reroll only makes sense on a queued up-next
  // no library jump when the slot is empty, or when View Details is layered underneath —
  // you came FROM the photo, and the gallery it would scroll+flash is covered by #detail
  fpvLib.hidden = !entry || !$("#detail").hidden;
}
// Exported: gallery badge taps and View Details rows (photo.js) open it too — one rule,
// "tap any photo×frame pairing", from every surface a pairing appears on.
export function openFramePreview(name, kind) {
  if (!screens()[name]) return;
  pvFrame = name; pvKind = (kind === "next" ? "next" : "now");
  fpvImg.dataset.src = "";   // force a fresh assignment for this open
  renderFramePreview();
  fpv.hidden = false;
  const focusEl = !fpvSkip.hidden ? fpvSkip : (!fpvLib.hidden ? fpvLib : fpv.querySelector("[data-fpv-close]"));
  setTimeout(() => focusEl && focusEl.focus(), 30);
}
function closeFramePreview() { if (!fpv.hidden) { fpv.hidden = true; pvFrame = null; } }

fpvImg.addEventListener("error", () => {
  if (!fpvImg.dataset.src) return;   // ignore the src-cleared (empty-slot) case
  fpvImg.classList.add("broken");
  fpvEmpty.hidden = false; fpvEmpty.textContent = "Preview unavailable";
});
fpv.querySelector(".fpv-toggle").addEventListener("click", (e) => {
  const b = e.target.closest(".fpv-seg"); if (!b) return;
  pvKind = b.dataset.kind === "next" ? "next" : "now";
  renderFramePreview();
});
fpvSkip.addEventListener("click", () => {
  const n = pvFrame; if (!n) return;
  const label = screens()[n]?.display_name || n;
  skipScreenNext(n)
    .then((resp) => { mutate((st) => { if (st.screens[n]) st.screens[n].next = resp.next_image; }); toast(`Skipped — up next changed on ${label}`); })
    .catch((err) => toast("Skip failed: " + err.message));
});
// "See photo in library": close the modal + frames drawer, then scroll to and flash the tile
fpvLib.addEventListener("click", () => {
  const sc = screens()[pvFrame]; if (!sc) return;
  const entry = fpvEntry(sc, pvKind); if (!entry) return;
  const color = frameColor(pvFrame), name = entry.name;
  closeFramePreview();   // dismiss the modal but leave the frames drawer floating open
  const sel = (window.CSS && CSS.escape) ? CSS.escape(name) : name.replace(/"/g, '\\"');
  // Closing the modal releases its scroll-lock on the next microtask, which restores the
  // pre-modal scroll position — so defer the scroll+flash one frame past that (rAF runs
  // after microtasks) or it gets instantly snapped back and nothing appears to happen.
  requestAnimationFrame(() => {
    const tile = document.querySelector(`#gallery .cell-tile[data-name="${sel}"]`);
    if (!tile) { toast(`“${name}” is in your library`); return; }   // filtered out of the current view
    tile.style.setProperty("--hc", color);
    // Land the tile fully BELOW the sticky stack (header bar + the still-open frames
    // drawer) — block:"center" could center it BEHIND the floating drawer, so the flash
    // played under the panel and nothing seemed to happen. If the tile sits so near the
    // top of the gallery that no scroll position can clear the drawer, close the drawer
    // (it animates 300ms and shifts layout, so re-run the measure after it settles).
    const land = () => {
      const panel = document.getElementById("frames-panel");
      const open = panel && panel.classList.contains("open");
      const bar = document.querySelector("header.bar");
      const clearTop = (open ? panel.getBoundingClientRect().bottom
                             : (bar ? bar.getBoundingClientRect().bottom : 0)) + 14;
      const target = window.scrollY + tile.getBoundingClientRect().top - clearTop;
      if (target < 0 && open) { panel.classList.remove("open"); setTimeout(land, 350); return; }
      window.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
      tile.classList.add("fpv-flash");
      setTimeout(() => tile.classList.remove("fpv-flash"), 2400);
    };
    land();
  });
});
fpv.addEventListener("click", (e) => { if (e.target === fpv || e.target.closest("[data-fpv-close]")) closeFramePreview(); });

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!fpv.hidden) {
    // Top-most layer takes the WHOLE press. The preview can be stacked over View Details,
    // and photo.js's Escape handler runs after this one (photo.js imports frames.js, so
    // this listener registered first) — without the stop, one press would see the preview
    // already closed and collapse the detail lightbox underneath it too.
    e.stopImmediatePropagation();
    closeFramePreview();
    return;
  }
  closeFrameMenu(); closeFrameDiag(); closeRename();
});
window.addEventListener("scroll", () => { if (!frameMenu.hidden) closeFrameMenu(); }, true);

subscribe((what) => { if (what === "status") { renderFrames(); if (!fpv.hidden) renderFramePreview(); } });
if (state.status) renderFrames();
