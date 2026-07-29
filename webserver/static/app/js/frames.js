// frames.js — the connected-frame cards (in the Frames drawer), the ⋯ menu, the
// diagnostics modal, remove-frame, per-frame orientation/match-orientation PATCH,
// and cancelling a per-frame pin. Ported from the mockup's renderFrames / frame-menu
// / openFrameDiag; the demo FRAMES array becomes state.status.screens, the canvas
// thumbnails become <img src=/thumbnail>, and every control hits the real backend.

import { state, subscribe, mutate } from "./state.js";
import { deleteScreen, patchScreen, clearScreenShowNext, thumbnailUrl, ditheredUrl, ditheredUrlMono } from "./api.js";
import { $, $$, esc, toast, fmtAgo, fmtUntil, fmtUptime, frameColor, armConfirm, disarm } from "./ui.js";

const fcards = $("#fcards");
const OVERDUE_GRACE_S = 120;   // wake jitter + clock drift allowance before "overdue"

const screens = () => state.status?.screens || {};
const entryByName = (name) => (state.status?.upload_files || []).find((e) => e.name === name) || null;

// The Frames drawer card MIRRORS the physical panel (not the generic upright /thumbnail):
// it shows the raw frame-oriented render, so a portrait photo on a landscape frame with
// auto-rotate OFF reads SIDEWAYS in the card exactly as the glass holds it (you turn the
// frame to view it). The card box is therefore always the FRAME's orientation, and the
// render is requested at that same orientation for both panel types.
const isMonoFrame = (sc) => sc?.panel_type === "mono16_e1003";
function frameContentPortrait(sc, entry) {
  // content orientation the frame composes (OFF keeps the photo's; ON follows the frame).
  const autoRotate = !!state.config?.config?.auto_rotate_fit;
  if (autoRotate) return sc.orientation === "portrait";
  return entry?.effective_orientation === "portrait";
}
function frameCardUrl(sc, entry) {
  if (!entry) return "";
  const framePortrait = sc.orientation === "portrait";
  if (isMonoFrame(sc)) {
    // raw=true → sideways wire decode (mirrors the glass), composed for the frame orientation.
    return ditheredUrlMono(entry, frameContentPortrait(sc, entry), true);
  }
  // colour: raw render at the frame's configured orientation (also sideways on mismatch).
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

  // Card box = the FRAME's orientation. Whether the card must rotate the image 90° differs
  // by panel type, because the two render SHAPES differ (verified):
  //   • COLOUR: ditheredUrl(orient=) returns an image SHAPED to that orientation
  //     (portrait→1200×1600, landscape→1600×1200), so it fills the box directly — NEVER rotate.
  //   • MONO (raw=1): the E1003 wire buffer is ALWAYS landscape-shaped (1872×1404) regardless
  //     of content — the physical panel is landscape. So for a PORTRAIT frame box the raw image
  //     is always landscape and object-fit:cover would CROP it. Rotate 90° in the card so it
  //     fills the portrait box with no crop (a portrait photo reads upright; a landscape photo
  //     reads sideways — exactly as the turned frame shows it). Landscape mono frame: buffer
  //     shape == box shape → no rotate.
  // Display-only, frame-preview thumbnails only.
  const thumb = el.querySelector(".fthumb");
  const boxPortrait = sc.orientation === "portrait";
  thumb.className = "fthumb shape-" + (boxPortrait ? "portrait" : "landscape");

  const rotateInCard = isMonoFrame(sc) && boxPortrait;   // mono raw buffer is always landscape
  const applyImg = (img, entry) => {
    setImg(img, entry ? frameCardUrl(sc, entry) : "");
    img.classList.toggle("sideways", rotateInCard && !!entry);
  };
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
    '<button class="ctx-item" data-fact="rename"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17z"/><path d="M13.5 6.5l3 3"/></svg><span>Rename</span></button>' +
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
  if (b.dataset.fact === "rename") { const n = menuFrame; closeFrameMenu(); openRename(n); }
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
  }
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

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeFrameMenu(); closeFrameDiag(); closeRename(); } });
window.addEventListener("scroll", () => { if (!frameMenu.hidden) closeFrameMenu(); }, true);

subscribe((what) => { if (what === "status") renderFrames(); });
if (state.status) renderFrames();
