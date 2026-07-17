// main.js — boot, poll loop, and the header/footer/drawer chrome.
// Views (gallery, photo, frames, settings, header tiles) plug in per milestone.

import { clearCache } from "./api.js";
import { state, subscribe, refreshStatus, refreshConfig, startPolling, mutate } from "./state.js";
import { $, $$, esc, toast, fmtBytes, fmtAgo, fmtClock, frameColor, isMobileVp, armConfirm } from "./ui.js";
import "./gallery.js";   // self-registers: filter tabs, slider/pinch, and the status→grid render
import "./photo.js";     // self-registers: tile gestures, detail lightbox, action menus/sheet
import "./header.js";    // self-registers: status tiles, uploads (XHR + drag-drop), failed modal
import "./frames.js";    // self-registers: frame cards, ⋯ menu, diagnostics, orientation PATCH, remove
import { openSettings } from "./settings.js";   // tabbed settings + custom dither editor

// ── header drawers: sticky toggles (stay open until their trigger is clicked again) ──
const DRAWERS = { server: "server-drawer", frames: "frames-panel" };
function openDrawer(key) {
  Object.entries(DRAWERS).forEach(([k, id]) => {
    const d = document.getElementById(id);
    if (k === key) d.classList.toggle("open");
    else d.classList.remove("open");
  });
}
function closeAllDrawers() {
  Object.values(DRAWERS).forEach((id) => document.getElementById(id).classList.remove("open"));
}

$("#brand").addEventListener("click", () => {
  // desktop = inline drawer; mobile = a clean modal (the drawer reads jumbled at phone width)
  isMobileVp() ? openServerModal() : openDrawer("server");
});
$("#fp").addEventListener("click", () => openDrawer("frames"));
$("#settings-btn").addEventListener("click", () => openSettings());
$("#edit-orientation").addEventListener("click", () => openSettings("frames"));
// #upload-btn + drag-drop are owned by header.js

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAllDrawers(); });

// ── scroll lock: freeze the page behind any open modal/sheet (iOS-reliable) ──
// One observer watches every full-screen surface's `hidden` attribute, so it stays in
// sync no matter which module opens/closes what. Desktop anchored menus are excluded —
// they intentionally close on scroll instead.
const LOCK_SURFACES = ["#detail", "#ctx", "#settings-modal", "#dither-modal", "#frame-modal", "#server-modal", "#failed-modal", "#pedit-overlay"];
let scrollLocked = false, savedScrollY = 0;
function syncScrollLock() {
  const open = LOCK_SURFACES.some((sel) => { const el = $(sel); return el && !el.hidden; });
  if (open && !scrollLocked) {
    savedScrollY = window.scrollY;
    // compensate for the vanishing scrollbar so desktop content doesn't jump sideways
    const sbw = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.top = `-${savedScrollY}px`;
    if (sbw > 0) document.body.style.paddingRight = `${sbw}px`;
    document.body.classList.add("modal-open");
    scrollLocked = true;
  } else if (!open && scrollLocked) {
    document.body.classList.remove("modal-open");
    document.body.style.top = "";
    document.body.style.paddingRight = "";
    window.scrollTo(0, savedScrollY);
    scrollLocked = false;
  }
}
const lockObserver = new MutationObserver(syncScrollLock);
LOCK_SURFACES.forEach((sel) => { const el = $(sel); if (el) lockObserver.observe(el, { attributes: true, attributeFilter: ["hidden"] }); });

// Clear cache — real action, two-step confirm (Clear-cache pattern)
const clearBtn = $("#clear-cache");
clearBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const mb = state.status ? fmtBytes(state.status.cache_used_bytes) : "cache";
  armConfirm(clearBtn, async () => {
    try {
      await clearCache();
      mutate();
      toast("Cache cleared — reconverting all images…");
    } catch (err) { toast("Clear cache failed: " + err.message); }
  }, `Confirm — clear ${mb}?`);
});

// ── chrome renders ──
function renderMeta(st) {
  const photos = st.upload_size ?? 0;
  const frames = Object.keys(st.screens || {}).length;
  $("#meta").innerHTML = `<b>${photos}</b> photo${photos === 1 ? "" : "s"} · <b>${frames}</b> frame${frames === 1 ? "" : "s"}`;
}

function renderFdots(st) {
  $("#fdots").innerHTML = Object.keys(st.screens || {})
    .map((n) => `<i style="--fc:${frameColor(n)}" title="${esc(n)}"></i>`).join("");
}

function renderServerDrawer(st) {
  const host = location.hostname + (location.port ? ":" + location.port : "");
  $("#svHost").textContent = host;
  $("#svIp").textContent = host;   // refined in M5 (server reports no IP; host is what the browser knows)
  const cfg = state.config;        // version/commit live in config — mirror the footer + mobile modal
  $("#svVersion").textContent = cfg?.git_describe || "—";
  $("#svCommit").textContent = cfg?.commit_url ? cfg.commit_url.split("/").pop().slice(0, 7) : "";
  $("#svCache").textContent = fmtBytes(st.cache_used_bytes);
  $("#svDisk").textContent = fmtBytes(st.disk_free_bytes);
  $("#svCpu").textContent = st.cpu_cores != null ? `${st.cpu_cores} cores` : "—";
  $("#svRam").textContent = st.memory_available_gb != null ? `${st.memory_available_gb} GB` : "—";
  $("#svWorkersLive").textContent = st.image_worker_count_resolved != null ? `${st.image_worker_count_resolved} active` : "—";
  const served = st.last_served || "—";
  const servedAt = served !== "—" && st.serve_data && st.serve_data[served] ? st.serve_data[served].last_request : null;
  $("#svServed").textContent = served;
  $("#svServed").nextElementSibling && ($("#svServed").nextElementSibling.textContent = servedAt ? fmtAgo(servedAt) : "");
}

// ── mobile server modal (same data as the desktop drawer, chunked into sections) ──
const serverModal = $("#server-modal");
function openServerModal() {
  const st = state.status, cfg = state.config;
  const host = location.hostname + (location.port ? ":" + location.port : "");
  const kv = (k, v) => `<span class="k">${esc(k)}</span><span class="v">${v}</span>`;
  const ver = cfg ? esc(cfg.git_describe || "—") + (cfg.commit_url ? ` <span class="commit">${esc(cfg.commit_url.split("/").pop().slice(0, 7))}</span>` : "") : "—";
  $("#server-modal-host").textContent = host;
  $("#server-modal-body").innerHTML =
    `<div class="sm-group"><div class="sm-h">Server</div><div class="dmeta">${
      kv("Address", `<span class="mono">${esc(host)}</span>`) + kv("Version", ver)}</div></div>` +
    `<div class="sm-group"><div class="sm-h">System</div><div class="dmeta">${
      kv("Disk free", st ? fmtBytes(st.disk_free_bytes) : "—") +
      kv("CPU", st && st.cpu_cores != null ? `${st.cpu_cores} cores` : "—") +
      kv("Free RAM", st && st.memory_available_gb != null ? `${st.memory_available_gb} GB` : "—") +
      kv("Workers", st && st.image_worker_count_resolved != null ? `${st.image_worker_count_resolved} active` : "—")}</div></div>` +
    `<div class="sm-group"><div class="sm-h">Activity</div><div class="dmeta">${
      kv("Server time", `<span class="mono">${st ? fmtClock(st.server_time) : "—"}</span>`) +
      kv("Cache", st ? fmtBytes(st.cache_used_bytes) : "—") +
      kv("Last served", st && st.last_served ? esc(st.last_served) : "—")}</div></div>`;
  serverModal.hidden = false;
}
function closeServerModal() { serverModal.hidden = true; }
serverModal.addEventListener("click", (e) => { if (e.target === serverModal || e.target.closest("[data-server-close]")) closeServerModal(); });
$("#smClear").addEventListener("click", (e) => {
  const mb = state.status ? fmtBytes(state.status.cache_used_bytes) : "cache";
  armConfirm(e.currentTarget, async () => {
    try { await clearCache(); mutate(); toast("Cache cleared — reconverting all images…"); }
    catch (err) { toast("Clear cache failed: " + err.message); }
  }, `Clear ${mb}?`);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeServerModal(); });

function renderFooter() {
  const st = state.status, cfg = state.config;
  if (st) $("#footTime").textContent = fmtClock(st.server_time);
  if (cfg) {
    $("#footVersion").textContent = cfg.git_describe || "—";
    const c = $("#footCommit");
    if (cfg.commit_url) {
      c.textContent = cfg.commit_url.split("/").pop().slice(0, 7);
      c.href = cfg.commit_url;
      c.hidden = false;
    } else c.hidden = true;
  }
}

// wordmark = the server's own name. `mdns_hostname` is just the "<name>" part of
// "<name>.local", so naming the server "maestro" makes the wordmark read "maestro.".
// Falls back to the product name when mDNS is off (no name set).
function renderBrand() {
  const name = (state.config?.config?.mdns_hostname || "").trim() || "hokku";
  const wtext = $("#brand .wtext");
  if (wtext) wtext.innerHTML = `${esc(name)}<i>.</i>`;
}

// offline indicator: amber wordmark dot + a fixed pill (never pushes content).
// Auto-recovers — the 5s poll keeps retrying and the next success clears it.
function setOffline(off) {
  document.body.classList.toggle("offline", off);
  $("#offline-pill").setAttribute("aria-hidden", String(!off));
}

subscribe((what) => {
  if (what === "status" && state.status) {
    setOffline(false);
    renderMeta(state.status);
    renderFdots(state.status);
    renderServerDrawer(state.status);
    renderFooter();
  }
  if (what === "config") { renderFooter(); renderBrand(); }
  if (what === "offline") setOffline(true);
});

// ── boot ──
(async function boot() {
  try { await refreshConfig(); } catch (e) { toast("Config load failed: " + e.message); }
  await refreshStatus();
  startPolling(5000);
})();
