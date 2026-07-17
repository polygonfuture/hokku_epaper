// settings.js — the tabbed Settings modal + the custom dither editor. Ported from
// the mockup's SETTINGS/showTab/refreshBody/…/openDither; the demo values become a
// live-cloned `draft` of state.config.config, presets are matched by deep equality,
// each tab saves a partial POST /config (then re-fetches), and the editor's live
// preview is the real server render (POST /dither/preview, debounced + aborted).
//
// The per-image crop/edit pipeline (mockup #pedit-*) is intentionally NOT ported —
// it's deferred; nothing here opens it.

import { state, refreshConfig, refreshStatus } from "./state.js";
import { postConfig, clearCache, clearClassifier, scrub, patchScreen, ditherPreview, thumbnailUrl } from "./api.js";
import { $, $$, esc, toast, isMobileVp, frameColor, armConfirm } from "./ui.js";

// ── config draft (clone on open; every control mutates it; Save POSTs a subset) ──
const clone = (o) => JSON.parse(JSON.stringify(o));
let draft = {};
function openDraft() { draft = state.config ? clone(state.config.config) : {}; }

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a == null || b == null) return false;
  if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  if (typeof a === "object") { const ka = Object.keys(a), kb = Object.keys(b); return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k])); }
  return false;
}
const presets = () => state.config?.dither_presets || {};
// a pipeline config matches a preset when every ImageConfig key equals the preset's
// (the preset also carries label/description, which we ignore)
function configMatchesPreset(cfg, preset) { return Object.keys(cfg).every((k) => deepEqual(cfg[k], preset[k])); }
function presetMatch(cfg) { if (!cfg) return null; for (const k of Object.keys(presets())) if (configMatchesPreset(cfg, presets()[k])) return k; return null; }
function presetConfig(key) { const { label, description, ...cfg } = presets()[key]; return clone(cfg); }

// ── tiny iOS-style builders (from the mockup) ──
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const fmtMin = (m) => (m < 60 ? m + "m" : m / 60 + "h");
const hmToInput = (hm) => (hm && hm.length === 4 ? hm.slice(0, 2) + ":" + hm.slice(2) : "");
const inputToHm = (v) => (v ? v.replace(":", "") : "");
const seq = (from, to, step) => { const a = []; for (let h = from; h <= to; h += step) a.push(String(h).padStart(2, "0") + "00"); return a; };
const iTog = (key, on) => `<button class="sw${on ? " on" : ""}" data-toggle="${key}" role="switch" aria-checked="${on ? "true" : "false"}"><span></span></button>`;
const iRow = (label, control) => `<div class="iset-row"><span class="iset-lab">${label}</span>${control}</div>`;
const iGroup = (inner, foot, footCls) => `<div class="igroup"><div class="iset">${inner}</div>${foot ? `<p class="iset-foot ${footCls || ""}">${foot}</p>` : ""}</div>`;

// ── reset-to-defaults (Detection + Image pages): restore the shipped presets/settings ──
const resetBtnHTML = () =>
  '<div class="set-reset"><button class="set-reset-btn" data-reset-defaults><span class="lbl">Reset to defaults</span></button>' +
  '<span class="set-reset-hint">Restores the presets and detection settings Hokku ships with.</span></div>';
// resets the ACTIVE tab's config keys to config_defaults (the server's fresh AppConfig),
// updates the draft + re-renders; Save then persists it.
function resetActiveTabToDefaults() {
  const defs = state.config ? state.config.config_defaults : null;
  const tab = SETTINGS[activeTab];
  if (!defs || !tab || !tab.keys.length) return;
  tab.keys.forEach((k) => { if (k in defs) draft[k] = clone(defs[k]); });
  renderTab(activeTab);
  toast("Reset to defaults — Save to keep");
}

// preset dropdown + "Custom…" for a pipeline (default / bw / face)
function presetControl(selId, pipelineKey, pipelineLabel) {
  const cur = presetMatch(draft[pipelineKey]);
  const opts = Object.entries(presets()).map(([k, p]) => `<option value="${k}"${k === cur ? " selected" : ""}>${esc(p.label || k)}</option>`).join("");
  const custom = cur ? "" : '<option value="custom" selected>Custom (edited)</option>';
  return `<span class="right"><select class="sel-input" id="${selId}" data-pipeline="${pipelineKey}">${custom}${opts}</select>` +
    `<button class="mini-btn" data-custom="${pipelineKey}" data-custom-label="${esc(pipelineLabel)}">Custom…</button></span>`;
}
function wirePreset(selId, pipelineKey, onChange) {
  const sel = $("#" + selId);
  sel.addEventListener("change", () => {
    if (sel.value === "custom") return;
    draft[pipelineKey] = presetConfig(sel.value);
    onChange && onChange();
  });
}

// ══ REFRESH ══
function refreshBody() {
  const chips = [15, 30, 60, 120, 240, 360, 720, 1440]
    .map((m) => `<button data-min="${m}"${m === draft.refresh_interval_minutes ? ' class="on"' : ""}>${fmtMin(m)}</button>`).join("");
  const presetBtns = [
    ["3&times; daily", "0600,1200,1800"], ["Every 2h", seq(0, 22, 2).join(",")],
    ["Hourly 8–22", seq(8, 22, 1).join(",")], ["Morning &amp; night", "0800,2000"],
  ].map(([lab, v]) => `<button data-times="${v}">${lab}</button>`).join("");
  const modeInterval = draft.refresh_mode === "interval";
  const activeOn = !!(draft.refresh_active_start || draft.refresh_active_end);
  return (
    '<div class="seg" id="rfMode">' +
      `<button data-mode="interval"${modeInterval ? ' class="on"' : ""}>Interval</button>` +
      `<button data-mode="times"${!modeInterval ? ' class="on"' : ""}>Specific times</button></div>` +
    '<div class="spanels">' +
    `<div class="spanel${modeInterval ? " on" : ""}" data-mode-panel="interval">` +
      `<div class="sfield"><div class="slabel">Refresh every</div><div class="chip-row" id="rfInterval">${chips}</div></div>` +
      '<div class="sw-row"><span>Only refresh during certain hours</span>' +
        `<button class="sw${activeOn ? " on" : ""}" id="rfActive" role="switch" aria-checked="${activeOn}"><span></span></button></div>` +
      `<div class="time-window${activeOn ? " on" : ""}" id="rfWindow">` +
        `<input type="time" value="${hmToInput(draft.refresh_active_start) || "07:00"}" id="rfStart"><span>to</span>` +
        `<input type="time" value="${hmToInput(draft.refresh_active_end) || "23:00"}" id="rfEnd"></div>` +
    "</div>" +
    `<div class="spanel${!modeInterval ? " on" : ""}" data-mode-panel="times">` +
      '<div class="sfield"><div class="slabel">Refresh at these times</div><div class="chip-row times" id="rfTimes"></div></div>' +
      '<div class="add-time"><input type="time" id="rfAddInput" value="09:00"><button id="rfAddBtn">Add time</button></div>' +
      `<div class="sfield"><div class="slabel">Presets</div><div class="chip-row" id="rfPresets">${presetBtns}</div></div>` +
    "</div></div>" +
    '<div class="spreview" id="rfPreview"></div>'
  );
}
function wireRefresh() {
  let times = [...(draft.refresh_image_at_time || [])];
  const renderTimes = () => {
    $("#rfTimes").innerHTML = times.length
      ? times.slice().sort().map((t) => `<button data-t="${t}" class="on">${hmToInput(t)}<span class="x">✕</span></button>`).join("")
      : '<span style="color:var(--faint);font-size:.82rem">No times set</span>';
  };
  const syncMode = () => {
    const mode = $("#rfMode .on").dataset.mode;
    draft.refresh_mode = mode;
    draft.refresh_interval_minutes = +$("#rfInterval .on")?.dataset.min || draft.refresh_interval_minutes;
    const active = $("#rfActive").classList.contains("on");
    draft.refresh_active_start = active ? inputToHm($("#rfStart").value) : "";
    draft.refresh_active_end = active ? inputToHm($("#rfEnd").value) : "";
    draft.refresh_image_at_time = times.slice().sort();
    preview();
  };
  const preview = () => {
    const mode = $("#rfMode .on").dataset.mode;
    let s;
    if (mode === "interval") {
      const min = +($("#rfInterval .on")?.dataset.min || draft.refresh_interval_minutes);
      const act = $("#rfActive").classList.contains("on");
      s = `Refreshes <b>every ${fmtMin(min)}</b>` + (act ? ` between <b>${$("#rfStart").value}</b> and <b>${$("#rfEnd").value}</b>.` : ", around the clock.");
    } else s = times.length ? `Refreshes at <b>${times.slice().sort().map(hmToInput).join(", ")}</b>.` : "No refresh times set.";
    $("#rfPreview").innerHTML = s;
  };
  $("#rfMode").addEventListener("click", (e) => { const b = e.target.closest("[data-mode]"); if (!b) return; $$("#rfMode button").forEach((x) => x.classList.toggle("on", x === b)); $$("[data-mode-panel]").forEach((p) => p.classList.toggle("on", p.dataset.modePanel === b.dataset.mode)); syncMode(); });
  $("#rfInterval").addEventListener("click", (e) => { const b = e.target.closest("[data-min]"); if (!b) return; $$("#rfInterval button").forEach((x) => x.classList.toggle("on", x === b)); syncMode(); });
  $("#rfActive").addEventListener("click", () => { const sw = $("#rfActive"), on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on); $("#rfWindow").classList.toggle("on", on); syncMode(); });
  $("#rfStart").addEventListener("input", syncMode);
  $("#rfEnd").addEventListener("input", syncMode);
  $("#rfTimes").addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (!b) return; times = times.filter((t) => t !== b.dataset.t); renderTimes(); syncMode(); });
  $("#rfAddBtn").addEventListener("click", () => { const t = inputToHm($("#rfAddInput").value); if (t && !times.includes(t)) { times.push(t); renderTimes(); syncMode(); } });
  $("#rfPresets").addEventListener("click", (e) => { const b = e.target.closest("[data-times]"); if (!b) return; times = b.dataset.times.split(","); renderTimes(); syncMode(); });
  renderTimes(); preview();
}

// ══ DETECTION ══
function detectBody() {
  const condRow = (when, label, control) => `<div class="iset-row cond" data-when="${when}"><span class="iset-lab">${label}</span>${control}</div>`;
  return (
    '<p class="set-desc">Hokku analyzes each photo before converting it — <b>black &amp; white</b> photos and photos with <b>faces</b> are routed to conversion pipelines tuned for each. Runs entirely on your server; nothing leaves your network.</p>' +
    '<div class="igroup"><div class="iset">' +
      iRow("Detect black &amp; white photos", iTog("bw", draft.classifier_bw_detect_enabled)) +
      condRow("bw", "B&amp;W preset", presetControl("bwPreset", "image_config_bw", "Black & White")) +
    '</div><p class="iset-foot">Detected B&amp;W photos use this pipeline — it skips the colour-boosting steps that would tint grays.</p></div>' +
    '<div class="igroup"><div class="iset">' +
      iRow("Detect faces", iTog("face", draft.classifier_face_detect_enabled)) +
      condRow("face", "Protect faces from CLAHE", iTog("clahe", draft.classifier_face_detect_clahe_keepout)) +
      condRow("face", "Face preset", presetControl("facePreset", "image_config_face", "Face")) +
    '</div><p class="iset-foot">Detected face photos use this pipeline — tuned to preserve natural skin tones.</p></div>' +
    resetBtnHTML()
  );
}
function wireDetect() {
  const TKEY = { bw: "classifier_bw_detect_enabled", face: "classifier_face_detect_enabled", clahe: "classifier_face_detect_clahe_keepout" };
  const sync = () => $$("[data-when]").forEach((row) => row.classList.toggle("off", !$(`[data-toggle="${row.dataset.when}"]`).classList.contains("on")));
  $$("[data-toggle]").forEach((sw) => sw.addEventListener("click", () => { const on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on); draft[TKEY[sw.dataset.toggle]] = on; sync(); }));
  wirePreset("bwPreset", "image_config_bw");
  wirePreset("facePreset", "image_config_face");
  sync();
}

// ══ IMAGE ══
function imageBody() {
  const cur = presetMatch(draft.image_config_default);
  const desc = cur ? presets()[cur].description || "" : "Custom pipeline (edited).";
  const fillPct = Math.round((draft.crop_to_fill_threshold ?? 0) * 100);
  return (
    iGroup(iRow("Default dither preset", presetControl("imPreset", "image_config_default", "Default")), `<span id="imPresetDesc">${esc(desc)}</span>`, "reserve2") +
    iGroup('<div class="iset-row"><span class="iset-lab">Zoom to fill</span><span class="range-val" id="imFillVal">' + fillPct + '%</span></div>' +
      `<div class="iset-slider"><input type="range" class="set-range" id="imFill" min="0" max="100" step="1" value="${fillPct}"></div>`,
      "Max zoom-in allowed to remove letterbox bars. 0% = always letterbox; higher crops more.") +
    resetBtnHTML()
  );
}
function wireImage() {
  const updDesc = () => { const cur = presetMatch(draft.image_config_default); $("#imPresetDesc").textContent = cur ? presets()[cur].description || "" : "Custom pipeline (edited)."; };
  wirePreset("imPreset", "image_config_default", updDesc);
  const fill = $("#imFill"), fv = $("#imFillVal");
  fill.addEventListener("input", () => { fv.textContent = fill.value + "%"; draft.crop_to_fill_threshold = +fill.value / 100; });
}

// ══ SERVER ══
function serverBody() {
  const wt = draft.image_worker_thread_count;
  const wsel = wt === 0 ? "auto" : wt === 1 ? "serial" : "custom";
  const mdnsOn = !!draft.mdns_hostname;
  return (
    iGroup(iRow("Poll interval", `<span class="unit-input"><input type="number" id="svPoll" min="1" max="3600" value="${draft.poll_interval_seconds ?? 10}"><span>sec</span></span>`),
      "How often the server checks the upload folder for new or removed images.") +
    iGroup(iRow("Debug screen", iTog("debug", draft.debug_fast_refresh)),
      "Forces every frame to refresh very frequently — drains battery. Off for normal use.") +
    iGroup(iRow("Auto-clear cache", iTog("autoclear", draft.auto_clear_cache)),
      "Deletes old cached conversions when disk runs low. Originals are never removed.") +
    iGroup(iRow("mDNS / Bonjour",
      `<span class="right"><span class="mdns-host${mdnsOn ? " on" : ""}" id="svMdnsHost"><input type="text" id="svHost" value="${esc(mdnsOn ? draft.mdns_hostname : "hokku")}"><span>.local</span></span>${iTog("mdns", mdnsOn)}</span>`),
      "Reach the server by name instead of its IP. Takes effect after a restart.") +
    iGroup(iRow("Image workers",
      `<span class="right"><select class="sel-input" id="svWorkers">` +
        `<option value="auto"${wsel === "auto" ? " selected" : ""}>Auto</option>` +
        `<option value="serial"${wsel === "serial" ? " selected" : ""}>Single threaded</option>` +
        `<option value="custom"${wsel === "custom" ? " selected" : ""}>Custom</option></select>` +
      `<span class="worker-custom${wsel === "custom" ? " on" : ""}" id="svWorkerCustom"><input type="number" id="svWorkerCount" min="1" max="1000" value="${wsel === "custom" ? wt : 4}"></span></span>`),
      "Parallel conversion threads. Auto = CPU cores − 1.") +
    '<div class="danger-row"><button class="danger-btn" id="svClear">Clear caches &amp; reconvert</button></div>'
  );
}
function wireServer() {
  const readWorkers = () => {
    const v = $("#svWorkers").value;
    draft.image_worker_thread_count = v === "auto" ? 0 : v === "serial" ? 1 : Math.max(1, +$("#svWorkerCount").value || 1);
  };
  $("#svPoll").addEventListener("input", () => { draft.poll_interval_seconds = Math.max(1, Math.min(3600, +$("#svPoll").value || 1)); });
  $$("[data-toggle]").forEach((sw) => sw.addEventListener("click", () => {
    const on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on);
    if (sw.dataset.toggle === "debug") draft.debug_fast_refresh = on;
    if (sw.dataset.toggle === "autoclear") draft.auto_clear_cache = on;
    if (sw.dataset.toggle === "mdns") { $("#svMdnsHost").classList.toggle("on", on); draft.mdns_hostname = on ? ($("#svHost").value.trim() || "hokku") : ""; }
  }));
  $("#svHost").addEventListener("input", () => { if ($("#svMdnsHost").classList.contains("on")) draft.mdns_hostname = $("#svHost").value.trim(); });
  $("#svWorkers").addEventListener("change", () => { $("#svWorkerCustom").classList.toggle("on", $("#svWorkers").value === "custom"); readWorkers(); });
  $("#svWorkerCount").addEventListener("input", readWorkers);
  // Clear caches & reconvert = classifier reset + cache clear (client-side two-step)
  const db = $("#svClear"); let armed = false, timer;
  const reset = () => { armed = false; db.textContent = "Clear caches & reconvert"; db.classList.remove("armed"); };
  db.addEventListener("click", async () => {
    if (!armed) { armed = true; db.textContent = "Confirm — clear & reconvert all?"; db.classList.add("armed"); timer = setTimeout(reset, 3500); return; }
    clearTimeout(timer); reset();
    try { await clearClassifier(); await clearCache(); await refreshStatus(); toast("Reconverting all images…"); }
    catch (e) { toast("Failed: " + e.message); }
  });
}

// ══ FRAMES (live PATCH — no config POST) ══
function framesBody() {
  const screens = state.status?.screens || {};
  const names = Object.keys(screens);
  if (!names.length) return '<p class="set-desc">No frames connected yet — a frame appears here the first time it wakes up and checks in with this server.</p>';
  return (
    "<p class=\"set-desc\">Each frame shows photos matching its shape. Pin a frame to <b>Landscape</b> or <b>Portrait</b>, and optionally only queue matching photos.</p>" +
    '<div class="igroup"><div class="iset">' +
      names.map((n) => {
        const sc = screens[n];
        return `<div class="iset-row frow" data-frame="${esc(n)}">` +
          `<span class="fnm"><span class="d" style="--fc:${frameColor(n)}"></span>${esc(n)}</span>` +
          '<div class="seg orient">' +
            ["landscape", "portrait"].map((o) => `<button data-orient="${o}"${sc.orientation === o ? ' class="on"' : ""}>${cap(o)}</button>`).join("") +
          "</div></div>" +
          `<div class="iset-row cond"><span class="iset-lab" style="padding-left:22px">Only matching photos</span>${iTog("match:" + n, sc.filter_by_orientation)}</div>`;
      }).join("") +
    "</div></div>"
  );
}
function wireFrames() {
  $$(".frow").forEach((row) => {
    const name = row.dataset.frame;
    row.querySelector(".seg.orient").addEventListener("click", (e) => {
      const b = e.target.closest("[data-orient]"); if (!b || b.classList.contains("on")) return;
      row.querySelectorAll(".seg.orient button").forEach((x) => x.classList.toggle("on", x === b));
      patchScreen(name, { orientation: b.dataset.orient }).then(() => refreshStatus()).catch((err) => { toast("Update failed: " + err.message); refreshStatus(); });
    });
  });
  $$('[data-toggle^="match:"]').forEach((sw) => sw.addEventListener("click", () => {
    const name = sw.dataset.toggle.slice(6), on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on);
    patchScreen(name, { filter_by_orientation: on }).then(() => refreshStatus()).catch((err) => { toast("Update failed: " + err.message); refreshStatus(); });
  }));
}

// ── settings shell ──
const svgIcon = (inner) => `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">${inner}</svg>`;
const SETTINGS = {
  refresh: { title: "Refresh Schedule", icon: svgIcon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'), body: refreshBody, wire: wireRefresh, keys: ["refresh_mode", "refresh_interval_minutes", "refresh_image_at_time", "refresh_active_start", "refresh_active_end"] },
  frames:  { title: "Frame Orientation", icon: svgIcon('<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>'), body: framesBody, wire: wireFrames, keys: [] },
  detect:  { title: "Smart Photo Detection", icon: svgIcon('<circle cx="9" cy="10" r="3"/><path d="M4 20a5 5 0 0 1 10 0"/><path d="M17 9h4M19 7v4"/>'), body: detectBody, wire: wireDetect, keys: ["classifier_bw_detect_enabled", "classifier_face_detect_enabled", "classifier_face_detect_clahe_keepout", "image_config_bw", "image_config_face"] },
  image:   { title: "Image Conversion & Color", icon: svgIcon('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 15l5-5 4 4 3-3 6 6"/>'), body: imageBody, wire: wireImage, keys: ["image_config_default", "crop_to_fill_threshold"] },
  server:  { title: "Server & Storage", icon: svgIcon('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>'), body: serverBody, wire: wireServer, keys: ["poll_interval_seconds", "debug_fast_refresh", "auto_clear_cache", "mdns_hostname", "image_worker_thread_count"] },
};
const setModal = $("#settings-modal"), setBody = $("#set-body"), setNav = $("#set-nav"), setBack = $("#set-back"), setTitle = $("#set-title");
let activeTab = "refresh";
function renderTab(key) {
  const cfg = SETTINGS[key]; if (!cfg) return;
  activeTab = key;
  setNav.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.tab === key));
  setBody.innerHTML = cfg.body();
  cfg.wire && cfg.wire();
  setBody.scrollTop = 0;
  if (isMobileVp()) { setBack.hidden = false; setTitle.textContent = cfg.title; }
  else { setBack.hidden = true; setTitle.textContent = "Settings"; }
}
function showSetList() {
  setBody.innerHTML = '<div class="set-list">' + Object.entries(SETTINGS).map(([k, c]) =>
    `<button class="set-li" data-li="${k}">${c.icon}<span>${c.title}</span><span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></button>`).join("") + "</div>";
  setBack.hidden = true; setTitle.textContent = "Settings"; setBody.scrollTop = 0;
}
export function openSettings(tab) {
  if (!state.config) { toast("Settings still loading…"); return; }
  openDraft();
  setNav.innerHTML = Object.entries(SETTINGS).map(([k, c]) => `<button data-tab="${k}" role="tab">${c.icon}${c.title}</button>`).join("");
  if (isMobileVp() && !tab) showSetList();
  else renderTab(tab || activeTab);
  setModal.hidden = false;
}
function closeSettings() { setModal.hidden = true; }
async function saveActiveTab() {
  const cfg = SETTINGS[activeTab];
  if (!cfg.keys.length) { closeSettings(); return; }   // frames tab PATCHes live
  const patch = {};
  cfg.keys.forEach((k) => { patch[k] = draft[k]; });
  try {
    await postConfig(patch);
    await refreshConfig();
    openDraft();   // re-sync from the server's normalized values
    toast("Settings saved");
    closeSettings();
  } catch (e) { toast("Save failed: " + e.message); }
}
setNav.addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) renderTab(b.dataset.tab); });
setBack.addEventListener("click", showSetList);
setModal.addEventListener("click", (e) => {
  if (e.target === setModal || e.target.closest("[data-set-close]")) { closeSettings(); return; }
  const li = e.target.closest("[data-li]"); if (li) renderTab(li.dataset.li);
});
$("#set-save").addEventListener("click", saveActiveTab);
setBody.addEventListener("click", (e) => {
  const rb = e.target.closest("[data-reset-defaults]");
  if (rb) { armConfirm(rb, resetActiveTabToDefaults, "Reset this page to defaults?"); return; }
  const b = e.target.closest("[data-custom]"); if (b) openDither(b.dataset.custom, b.dataset.customLabel);
});

// ══ Custom dither editor (desktop) — knobs edit a working copy; live server preview ══
const DR = (key, label, min, max, step) => ({ key, label, t: "r", min, max, step });
const DS = (key, label, opts) => ({ key, label, t: "s", opts });
const DC = (key, label) => ({ key, label, t: "c" });
const DITHER_NESTED = new Set(["lut_name", "hue_cutoff_deg", "neutral_chroma", "algorithm", "serpentine"]);
const getKnob = (cfg, key) => (DITHER_NESTED.has(key) ? cfg.dither?.[key] : cfg[key]);
function setKnob(cfg, key, v) { if (DITHER_NESTED.has(key)) { (cfg.dither ||= {})[key] = v; } else cfg[key] = v; }
const DITHER_STAGES = [
  { title: "Tonal preparation", desc: "Exposure, contrast, gamma & sharpening before dithering", knobs: [
    DR("prepare_autocontrast_cutoff", "Autocontrast cutoff", 0, 1, 0.05), DR("prepare_gamma", "Gamma", 0.48, 1.28, 0.02),
    DR("prepare_midtone", "Midtone lift", 0.62, 1.42, 0.02), DR("prepare_brightness", "Brightness", 0.5, 1.5, 0.02),
    DR("prepare_contrast", "Contrast", 0.6, 1.6, 0.02), DR("clahe_clip_limit", "Local contrast (CLAHE)", 0, 3.5, 0.05),
    DR("clahe_keepout_feather", "Keepout feather", 0, 0.03, 0.001), DR("prepare_usm_amount", "Sharpening amount", 0, 240, 5),
    DR("prepare_usm_radius", "Sharpening radius", 0.2, 1.8, 0.05), DR("dither_noise", "Pre-dither noise", 0, 4, 0.1),
  ] },
  { title: "Colour enhancement", desc: "Saturation & colour boosting across the image", knobs: [
    DR("color_enhance", "Global colour enhance", 0.5, 2, 0.05),
    DS("adaptive_saturate_space", "Adaptive saturation", [["off", "Off"], ["cielab", "CIELAB"], ["oklab", "OKLAB"]]),
    DR("saturate_max_enhance", "Max enhance", 0.5, 2, 0.05), DR("saturate_low_chroma_thresh", "Low chroma (CIELAB)", 0, 10, 0.5),
    DR("saturate_high_chroma_thresh", "High chroma (CIELAB)", 5, 25, 0.5), DR("saturate_low_chroma_thresh_oklab", "Low chroma (OKLAB)", 0, 0.05, 0.005),
    DR("saturate_high_chroma_thresh_oklab", "High chroma (OKLAB)", 0.025, 0.125, 0.005),
  ] },
  { title: "Dynamic range compression", desc: "Lightness & chroma compression to fit the 6-ink gamut", knobs: [
    DS("drc_l_space", "L compression space", [["cielab", "CIELAB"], ["oklab", "OKLAB"]]), DS("drc_chroma_space", "Chroma scaling space", [["cielab", "CIELAB"], ["oklab", "OKLAB"]]),
    DC("scale_chroma", "Scale chroma"), DC("adaptive_vivid", "Adaptive vivid"),
    DR("vivid_chroma_low", "Vivid low (CIELAB)", 0, 10, 0.5), DR("vivid_chroma_high", "Vivid high (CIELAB)", 5, 25, 0.5),
    DR("vivid_chroma_low_oklab", "Vivid low (OKLAB)", 0, 0.05, 0.005), DR("vivid_chroma_high_oklab", "Vivid high (OKLAB)", 0.025, 0.125, 0.005),
  ] },
  { title: "Palette LUT", desc: "How colours are matched to the six Spectra inks", knobs: [
    DS("lut_name", "LUT", [["euclidean", "CIELAB"], ["euclidean_weighted", "CIELAB weighted"], ["hue_aware", "CIELAB hue-aware"], ["hue_aware_weighted", "CIELAB hue-aware weighted"], ["oklab", "OKLAB"], ["oklab_hue_aware", "OKLAB hue-aware"], ["cam16ucs", "CAM16-UCS"], ["cam16ucs_hue_aware", "CAM16-UCS hue-aware"], ["bw", "B&amp;W only"]]),
    DR("hue_cutoff_deg", "Hue cutoff °", 10, 180, 1), DR("neutral_chroma", "Neutral chroma", 0, 16, 0.5),
  ] },
  { title: "Dither kernel", desc: "The error-diffusion algorithm & scan pattern", knobs: [
    DS("algorithm", "Algorithm", [["floyd_steinberg", "Floyd–Steinberg"], ["atkinson", "Atkinson"], ["stucki", "Stucki"]]), DC("serpentine", "Serpentine scan"),
  ] },
];
const KNOB_DEFAULTS = {};   // filled from image_config_default so "tweaked" + Reset work against the base
function computeKnobDefaults() { const base = state.config?.config?.image_config_default; if (base) DITHER_STAGES.forEach((s) => s.knobs.forEach((k) => KNOB_DEFAULTS[k.key] = getKnob(base, k.key))); }

const ditherModal = $("#dither-modal");
let editPipeline = null, editCfg = null, ditherStage = 0, editSample = null, previewTimer = null, previewCtrl = null, previewUrl = null;
const ditherPhotos = () => (state.status?.upload_files || []).filter((e) => e.status === "ok").slice(-5);

function ditherKnobHTML(k) {
  const v = getKnob(editCfg, k.key), def = KNOB_DEFAULTS[k.key];
  const lab = `<span class="dknob-lab">${k.label}</span>`;
  if (k.t === "r") return `<div class="dknob${v !== def ? " tweaked" : ""}"><div class="dknob-head">${lab}<span class="dknob-val" data-val="${k.key}">${v}</span></div><div class="dtrack"><input type="range" class="dslider" data-knob="${k.key}" min="${k.min}" max="${k.max}" step="${k.step}" value="${v}"></div></div>`;
  if (k.t === "s" && k.opts.length <= 3) return `<div class="dknob"><div class="dknob-head">${lab}</div><div class="dseg" data-knob="${k.key}">${k.opts.map((o) => `<button data-opt="${o[0]}"${o[0] === v ? ' class="on"' : ""}>${o[1]}</button>`).join("")}</div></div>`;
  if (k.t === "s") return `<div class="dknob"><div class="dknob-head">${lab}</div><select class="sel-input" data-knob="${k.key}">${k.opts.map((o) => `<option value="${o[0]}"${o[0] === v ? " selected" : ""}>${o[1]}</option>`).join("")}</select></div>`;
  return `<div class="dknob row">${lab}<button class="sw${v ? " on" : ""}" data-knob="${k.key}" role="switch" aria-checked="${!!v}"><span></span></button></div>`;
}
function renderDitherCats() {
  $("#dcats").innerHTML = DITHER_STAGES.map((st, i) => `<button class="dcat${i === ditherStage ? " on" : ""}" data-stage="${i}"><span class="dc-t">${st.title}</span><span class="dc-d">${st.desc}</span></button>`).join("");
}
function renderDitherControls() {
  const ctrl = $("#dcontrols"), stage = DITHER_STAGES[ditherStage];
  ctrl.innerHTML = stage.knobs.map(ditherKnobHTML).join("");
  ctrl.querySelectorAll(".dslider[data-knob]").forEach((sl) => sl.addEventListener("input", () => {
    const key = sl.dataset.knob, v = +sl.value; setKnob(editCfg, key, v);
    ctrl.querySelector(`[data-val="${key}"]`).textContent = v;
    sl.closest(".dknob").classList.toggle("tweaked", v !== KNOB_DEFAULTS[key]);
    schedulePreview();
  }));
  ctrl.querySelectorAll(".dseg[data-knob]").forEach((seg) => seg.addEventListener("click", (e) => { const b = e.target.closest("[data-opt]"); if (!b) return; setKnob(editCfg, seg.dataset.knob, b.dataset.opt); seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); schedulePreview(); }));
  ctrl.querySelectorAll("select[data-knob]").forEach((se) => se.addEventListener("change", () => { setKnob(editCfg, se.dataset.knob, se.value); schedulePreview(); }));
  ctrl.querySelectorAll(".sw[data-knob]").forEach((sw) => sw.addEventListener("click", () => { const on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on); setKnob(editCfg, sw.dataset.knob, on); schedulePreview(); }));
}
function renderDitherPicker() {
  const pics = ditherPhotos();
  $("#dprev-pick").innerHTML = pics.map((t) => `<button data-sample="${esc(t.name)}"${editSample && editSample.name === t.name ? ' class="on"' : ""} title="${esc(t.name)}"><img alt="" src="${thumbnailUrl(t)}"></button>`).join("");
}
// debounced server preview: at most one request in flight (AbortController)
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 400);
}
async function runPreview() {
  if (!editSample) return;
  if (previewCtrl) previewCtrl.abort();
  previewCtrl = new AbortController();
  $("#dprev-stage").classList.add("busy");
  try {
    const { blobUrl, faceBboxes } = await ditherPreview(editSample.name, editCfg, undefined, previewCtrl.signal);
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = blobUrl;
    const img = $("#dprev-img"); img.classList.remove("broken"); img.src = blobUrl;
    const layer = $("#dprev-faces");
    if (faceBboxes && faceBboxes.length) {
      layer.hidden = false;
      layer.innerHTML = faceBboxes.map((b) => `<i style="left:${b[0] * 100}%;top:${b[1] * 100}%;width:${b[2] * 100}%;height:${b[3] * 100}%"></i>`).join("");
    } else { layer.hidden = true; layer.innerHTML = ""; }
    $("#dprev-stage").classList.remove("busy");
  } catch (e) {
    if (e.name === "AbortError") return;   // superseded by a newer request
    $("#dprev-stage").classList.remove("busy");
    $("#dprev-img").classList.add("broken");
    toast("Preview failed: " + e.message);
  }
}
function openDither(pipelineKey, pipelineLabel) {
  if (isMobileVp()) { toast("The custom dither editor is desktop-only."); return; }
  computeKnobDefaults();
  editPipeline = pipelineKey;
  editCfg = clone(draft[pipelineKey]);
  ditherStage = 0;
  editSample = ditherPhotos()[0] || null;
  $("#dither-title").innerHTML = `Custom dither <span class="dt-sub">· ${esc(pipelineLabel || "")}</span>`;
  renderDitherCats(); renderDitherControls(); renderDitherPicker();
  const img = $("#dprev-img"); img.removeAttribute("src"); $("#dprev-faces").hidden = true;
  ditherModal.hidden = false;
  if (editSample) runPreview(); else toast("Upload a photo to preview the dither.");
}
function closeDither() { ditherModal.hidden = true; clearTimeout(previewTimer); if (previewCtrl) previewCtrl.abort(); }
ditherModal.addEventListener("click", (e) => { if (e.target === ditherModal || e.target.closest("[data-dither-close]")) closeDither(); });
$("#dcats").addEventListener("click", (e) => { const b = e.target.closest("[data-stage]"); if (!b) return; ditherStage = +b.dataset.stage; renderDitherCats(); renderDitherControls(); });
$("#dprev-pick").addEventListener("click", (e) => { const b = e.target.closest("[data-sample]"); if (!b) return; editSample = ditherPhotos().find((t) => t.name === b.dataset.sample); renderDitherPicker(); runPreview(); });
$("#dither-reset").addEventListener("click", () => { editCfg = clone(state.config.config[editPipeline]); renderDitherControls(); runPreview(); toast("Reset to saved pipeline"); });
$("#dither-save").addEventListener("click", () => {
  draft[editPipeline] = clone(editCfg);
  closeDither();
  renderTab(activeTab);   // reflect "Custom" in the preset dropdown
  toast("Dither applied — press Save to keep it");
});

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (!ditherModal.hidden) closeDither(); else closeSettings(); } });
