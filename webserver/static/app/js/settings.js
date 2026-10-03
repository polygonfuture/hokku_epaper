// settings.js — the tabbed Settings modal + the custom dither editor. Ported from
// the mockup's SETTINGS/showTab/refreshBody/…/openDither; the demo values become a
// live-cloned `draft` of state.config.config, presets are matched by deep equality,
// each tab saves a partial POST /config (then re-fetches), and the editor's live
// preview is the real server render (POST /dither/preview, debounced + aborted).
//
// The per-image crop/edit pipeline (mockup #pedit-*) is intentionally NOT ported —
// it's deferred; nothing here opens it.

import { state, refreshConfig, refreshStatus } from "./state.js";
import { postConfig, clearCache, clearClassifier, scrub, patchScreen, ditherPreview, monoPreview, thumbnailUrl } from "./api.js";
import { $, $$, esc, toast, isMobileVp, frameColor, armConfirm } from "./ui.js";
import { stageZoom } from "./stage-zoom.js";

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

// ── reset-to-defaults (Image Rendering page): restore the shipped presets/settings ──
const resetBtnHTML = (hint) =>
  '<div class="set-reset"><button class="set-reset-btn" data-reset-defaults><span class="lbl">Reset to defaults</span></button>' +
  `<span class="set-reset-hint">${hint}</span></div>`;
// resets the ACTIVE tab's config keys to config_defaults (the server's fresh AppConfig),
// updates the draft + re-renders; Save then persists it. A tab's `resetKeys` (when set)
// narrows what Reset touches — Image Rendering saves the E1003 keys but never resets them.
function resetActiveTabToDefaults() {
  const defs = state.config ? state.config.config_defaults : null;
  const tab = SETTINGS[activeTab];
  const keys = tab ? tab.resetKeys || tab.keys : [];
  if (!defs || !keys.length) return;
  keys.forEach((k) => { if (k in defs) draft[k] = clone(defs[k]); });
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

// ══ IMAGE RENDERING ══
// One page for how every image is converted: a section per kind of image (each with its
// dither preset, under the switch that turns it on), cropping & framing, and — only when
// one is connected — the E1003 look. Labels follow upstream's "Image rendering" section
// where they can (Dither preset, Face-aware cropping); the text is ours, in plain words.
// The page's Reset never touches the E1003 look: Custom's settings reset in the tone editor.
const secLabel = (text) => `<p class="iset-sec">${text}</p>`;
const condRow = (when, label, control) => `<div class="iset-row cond" data-when="${when}"><span class="iset-lab">${label}</span>${control}</div>`;
const RENDER_TOGGLES = { bw: "classifier_bw_detect_enabled", face: "classifier_face_detect_enabled", clahe: "classifier_face_detect_clahe_keepout", facecrop: "classifier_face_aware_crop_enabled" };
const RENDER_KEYS = ["image_config_default", "image_config_face", "image_config_bw", ...Object.values(RENDER_TOGGLES), "crop_to_fill_threshold"];
const presetDescText = () => { const cur = presetMatch(draft.image_config_default); return cur ? presets()[cur].description || "" : "Custom pipeline (edited)."; };
function renderBody() {
  const fillPct = Math.round((draft.crop_to_fill_threshold ?? 0) * 100);
  const profile = draft.mono_e1003_profile || "faithful";
  const monoOpts = MONO_PROFILES.map(([v, l]) => `<option value="${v}"${v === profile ? " selected" : ""}>${l}</option>`).join("");
  return (
    '<p class="set-desc">Hokku checks each image for <b>faces</b> and <b>black &amp; white</b>, then converts it with the pipeline for that type. All on your server; nothing leaves your network.</p>' +
    secLabel("Standard images") +
    iGroup(iRow("Dither preset", presetControl("imPreset", "image_config_default", "Standard images")),
      `For images that aren't black &amp; white or portraits.<span class="foot-desc" id="imPresetDesc">${esc(presetDescText())}</span>`) +
    secLabel("Portraits") +
    iGroup(
      iRow("Detect faces", iTog("face", draft.classifier_face_detect_enabled)) +
      condRow("face", "Dither preset", presetControl("facePreset", "image_config_face", "Portraits")) +
      condRow("face", 'Protect faces from local contrast (CLAHE)<span class="sw-hint">Keeps the contrast boost off skin</span>', iTog("clahe", draft.classifier_face_detect_clahe_keepout)),
      "For images with faces, tuned so skin doesn't turn orange or grey on e-ink.") +
    secLabel("Black &amp; white images") +
    iGroup(
      iRow("Detect B&amp;W images", iTog("bw", draft.classifier_bw_detect_enabled)) +
      condRow("bw", "Dither preset", presetControl("bwPreset", "image_config_bw", "Black & white images")),
      "For black-and-white images like film scans and monochrome art. Skips colour boosting so grays don't tint pink or yellow. Also used for black-and-white images with faces.") +
    secLabel("Cropping &amp; framing") +
    // one card: the slider, then the switch that only acts when it crops
    iGroup(
      '<div class="iset-row"><span class="iset-lab">Zoom to fill</span><span class="range-val" id="imFillVal">' + fillPct + "%</span></div>" +
      `<div class="iset-slider"><input type="range" class="set-range" id="imFill" min="0" max="150" step="1" value="${fillPct}" aria-label="Zoom to fill"></div>` +
      // greys out (stays visible) while face detection is off — it uses the detected faces
      '<div class="iset-row dim" data-dim="face"><span class="iset-lab">Face-aware cropping<span class="sw-hint">Crops around faces so zooming won\'t cut off heads. Needs face detection (Portraits).</span></span>' +
        `${iTog("facecrop", !!draft.classifier_face_aware_crop_enabled)}</div>`,
      "How far Hokku may zoom in, cropping the edges, instead of showing white letterbox bars when an image isn't the frame's 4:3 shape. 0% always shows bars; at 100%, half the image is cropped away. Editor crops already fit." +
      '<span class="foot-ref">Zoom needed: 13% for 3:2 images, 33% for 16:9 or square. For a landscape image on a portrait frame (or the reverse): 78% for 4:3, 100% for 3:2, 137% for 16:9.</span>') +
    (hasMonoPanel()
      ? secLabel("E1003 Mono") +
        iGroup(iRow("Tone profile", `<span class="right"><select class="sel-input" id="moPreset" aria-label="E1003 tone profile">${monoOpts}</select>` +
            '<button class="mini-btn" data-mono-custom>Edit custom…</button></span>'),
          "The look of the 16-grey E1003 panel. Faithful (the classic look) and B&amp;W Contrast (filmic) are fixed; Custom is yours to edit, with a live preview.")
      : "") +
    resetBtnHTML("Restores presets, detection and cropping. Leaves the E1003 look alone.")
  );
}
function wireRender() {
  const isOn = (key) => !!$(`[data-toggle="${key}"]`, setBody)?.classList.contains("on");
  const sync = () => {
    $$("[data-when]", setBody).forEach((row) => row.classList.toggle("off", !isOn(row.dataset.when)));
    $$("[data-dim]", setBody).forEach((row) => {
      const off = !isOn(row.dataset.dim);
      row.classList.toggle("off", off);
      const sw = row.querySelector(".sw"); sw.disabled = off; sw.setAttribute("aria-disabled", off);
    });
  };
  $$(".sw[data-toggle]", setBody).forEach((sw) => sw.addEventListener("click", () => {
    if (sw.disabled) return;
    const on = sw.classList.toggle("on"); sw.setAttribute("aria-checked", on);
    draft[RENDER_TOGGLES[sw.dataset.toggle]] = on;
    sync();
  }));
  wirePreset("imPreset", "image_config_default", () => { $("#imPresetDesc").textContent = presetDescText(); });
  wirePreset("facePreset", "image_config_face");
  wirePreset("bwPreset", "image_config_bw");
  const fill = $("#imFill"), fv = $("#imFillVal");
  fill.addEventListener("input", () => { fv.textContent = fill.value + "%"; draft.crop_to_fill_threshold = +fill.value / 100; });
  const mono = $("#moPreset");
  if (mono) mono.addEventListener("change", () => { draft.mono_e1003_profile = mono.value; if (mono.value === "custom") openMono(); });
  sync();
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
    iGroup(iRow("Serve-decision log", iTog("servelog", draft.serve_decision_log_enabled)),
      "Writes serve_decisions.jsonl (up-next vs actually-served) for debugging. Off for normal use.") +
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
    if (sw.dataset.toggle === "servelog") draft.serve_decision_log_enabled = on;
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
    // one statement per line, so the bold "Only matching images" never runs into "Portrait"
    '<p class="set-desc"><span class="dline">Set each frame to <b>Landscape</b> or <b>Portrait</b>.</span>' +
      '<span class="dline"><b>Only matching images</b> sends it only images of that shape.</span>' +
      '<span class="dline">Letterbox bars and zoom are set in <b>Image Rendering → Cropping &amp; framing</b>.</span></p>' +
    '<div class="igroup"><div class="iset">' +
      names.map((n) => {
        const sc = screens[n];
        return `<div class="iset-row frow" data-frame="${esc(n)}">` +
          `<span class="fnm"><span class="d" style="--fc:${frameColor(n)}"></span>${esc(n)}</span>` +
          '<div class="seg orient">' +
            ["landscape", "portrait"].map((o) => `<button data-orient="${o}"${sc.orientation === o ? ' class="on"' : ""}>${cap(o)}</button>`).join("") +
          "</div></div>" +
          `<div class="iset-row cond"><span class="iset-lab" style="padding-left:22px">Only matching images</span>${iTog("match:" + n, sc.filter_by_orientation)}</div>`;
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

// ══ E1003 MONO (a section of Image Rendering, shown only when a mono16_e1003 panel is connected) ══
const hasMonoPanel = () => Object.values(state.status?.screens || {}).some((s) => s.panel_type === "mono16_e1003");
// The two on-glass-validated panel looks (see mono_e1003.py). A "preset" here is
// the full flat mono knob set; "Custom…" opens the mono tone editor (a mono-tailored
// twin of the colour dither editor) to tweak the same knobs with a live panel
// preview. Named to match how they READ ON GLASS (user-validated):
//   • Faithful = UNIFORM ramp — smooth, open gradients, lighter/accurate. The
//     recommended default (this is the live config's look).
//   • Punchy   = MEASURED ramp — darker, deeper/crunchier blacks, more contrast.
// (The measured ramp reads dark on this unit; the earlier labelling had these two
//  swapped because a decode bug showed the measured render as soft — fixed.)
// no-op tone defaults shared by both presets — a preset is a full look, so its
// tone knobs sit at neutral; touching any of them flips the dropdown to "Custom".
// E1003 tone PROFILE selector. The dropdown only switches mono_e1003_profile —
// it never overwrites the Custom control values (those persist independently):
//   • Faithful    = the frozen legacy ramp render (never edited).
//   • B&W Contrast = the fixed darktable+Lightroom-matched preset.
//   • Custom       = the editable dtcore controls (opens the 3-page editor).
const MONO_PROFILES = [["faithful", "Faithful"], ["bw_contrast", "B&W Contrast"], ["custom", "Custom…"]];
// Config keys the mono section owns: the profile + the editable dtcore/sharpen
// controls. Faithful's legacy ramp keys stay frozen in config and aren't touched.
const MONO_KEYS = [
  "mono_e1003_profile",
  "mono_e1003_sig_enabled", "mono_e1003_sig_contrast", "mono_e1003_sig_skew", "mono_e1003_sig_white", "mono_e1003_sig_black",
  "mono_e1003_lc_enabled", "mono_e1003_lc_detail", "mono_e1003_lc_highlights", "mono_e1003_lc_shadows", "mono_e1003_lc_midtone",
  "mono_e1003_basic_enabled", "mono_e1003_basic_exposure", "mono_e1003_basic_contrast", "mono_e1003_basic_highlights",
  "mono_e1003_basic_shadows", "mono_e1003_basic_whites", "mono_e1003_basic_blacks", "mono_e1003_basic_clahe",
  "mono_e1003_sharpen_amount", "mono_e1003_sharpen_radius", "mono_e1003_sharpen_threshold",
];

// ── settings shell ──
const svgIcon = (inner) => `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">${inner}</svg>`;
// Sections whose `gate` returns false are hidden (capability-gating).
const settingsEntries = () => Object.entries(SETTINGS).filter(([, c]) => !c.gate || c.gate());
const SETTINGS = {
  refresh: { title: "Refresh Schedule", icon: svgIcon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'), body: refreshBody, wire: wireRefresh, keys: ["refresh_mode", "refresh_interval_minutes", "refresh_image_at_time", "refresh_active_start", "refresh_active_end"] },
  frames:  { title: "Frame Orientation", icon: svgIcon('<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8"/>'), body: framesBody, wire: wireFrames, keys: [] },
  // Saves the E1003 keys too (its section lives here) but Reset leaves them alone.
  image:   { title: "Image Rendering", icon: svgIcon('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 15l5-5 4 4 3-3 6 6"/>'), body: renderBody, wire: wireRender, keys: [...RENDER_KEYS, ...MONO_KEYS], resetKeys: RENDER_KEYS },
  server:  { title: "Server & Storage", icon: svgIcon('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>'), body: serverBody, wire: wireServer, keys: ["poll_interval_seconds", "debug_fast_refresh", "serve_decision_log_enabled", "auto_clear_cache", "mdns_hostname", "image_worker_thread_count"] },
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
  setBody.innerHTML = '<div class="set-list">' + settingsEntries().map(([k, c]) =>
    `<button class="set-li" data-li="${k}">${c.icon}<span>${c.title}</span><span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></button>`).join("") + "</div>";
  setBack.hidden = true; setTitle.textContent = "Settings"; setBody.scrollTop = 0;
}
export function openSettings(tab) {
  if (!state.config) { toast("Settings still loading…"); return; }
  openDraft();
  setNav.innerHTML = settingsEntries().map(([k, c]) => `<button data-tab="${k}" role="tab">${c.icon}${c.title}</button>`).join("");
  // If the active tab is gated off (e.g. no mono panel), fall back to the first visible one.
  let target = tab || activeTab;
  if (!SETTINGS[target] || (SETTINGS[target].gate && !SETTINGS[target].gate())) target = settingsEntries()[0][0];
  if (isMobileVp() && !tab) showSetList();
  else renderTab(target);
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
  const b = e.target.closest("[data-custom]"); if (b) { openDither(b.dataset.custom, b.dataset.customLabel); return; }
  const m = e.target.closest("[data-mono-custom]"); if (m) openMono();
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
// the shown render: whether it's full panel size (for 100% zoom), and its trim geometry
let previewFull = false, previewGeom = null;
const DITHER_DRAFT_SIDE = 800, DITHER_FULL_SIDE = 1600;   // the colour panel is 1600 x 1200
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
  ctrl.querySelectorAll(".dslider[data-knob]").forEach((sl) => {
    const key = sl.dataset.knob;
    const apply = (v) => {
      setKnob(editCfg, key, v); sl.value = v;
      ctrl.querySelector(`[data-val="${key}"]`).textContent = v;
      sl.closest(".dknob").classList.toggle("tweaked", v !== KNOB_DEFAULTS[key]);
      schedulePreview();
    };
    sl.addEventListener("input", () => apply(+sl.value));
    sl.addEventListener("dblclick", () => apply(KNOB_DEFAULTS[key]));   // double-click a slider to reset it to default
  });
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
// The render comes back letterboxed onto the panel canvas: pure-white bars on two sides
// for some photos, none for others. These editors (dither and E1003 tone) are for judging
// the render, so trim the bars off and let CSS frame every photo in the same even white
// mat instead. letterboxBox finds the photo's box (fractions of the canvas) in a decoded
// render; cropToBox cuts that box out into a new blob URL.
function letterboxBox(src) {
  const W = src.naturalWidth || src.width, H = src.naturalHeight || src.height;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(src, 0, 0);
  const d = g.getImageData(0, 0, W, H).data;
  const white = (x, y) => { const i = (y * W + x) * 4; return d[i] >= 250 && d[i + 1] >= 250 && d[i + 2] >= 250; };
  const colWhite = (x) => { for (let y = 0; y < H; y++) if (!white(x, y)) return false; return true; };
  const rowWhite = (y) => { for (let x = 0; x < W; x++) if (!white(x, y)) return false; return true; };
  let l = 0, r = W, t = 0, b = H;
  while (r - l > 1 && colWhite(l)) l++;
  while (r - l > 1 && colWhite(r - 1)) r--;
  while (b - t > 1 && rowWhite(t)) t++;
  while (b - t > 1 && rowWhite(b - 1)) b--;
  // land: the panel canvas it was rendered onto — landscape (1600 x 1200, E1003 1872 x 1404) or portrait
  return { box: [l / W, t / H, (r - l) / W, (b - t) / H], land: W >= H };
}
// draws synchronously (the source may be released once this returns its promise), then encodes
async function cropToBox(src, box) {
  const W = src.naturalWidth || src.width, H = src.naturalHeight || src.height;
  const l = Math.round(box[0] * W), t = Math.round(box[1] * H), w = Math.round(box[2] * W), h = Math.round(box[3] * H);
  const o = document.createElement("canvas"); o.width = w; o.height = h;
  o.getContext("2d").drawImage(src, l, t, w, h, 0, 0, w, h);
  const blob = await new Promise((res) => o.toBlob(res, "image/png"));
  return URL.createObjectURL(blob);
}
async function trimLetterbox(blobUrl) {
  const im = new Image(); im.src = blobUrl; await im.decode();
  const { box, land } = letterboxBox(im);
  if (box[0] === 0 && box[1] === 0 && box[2] === 1 && box[3] === 1) return { url: blobUrl, box, land };
  const url = await cropToBox(im, box);
  URL.revokeObjectURL(blobUrl);
  return { url, box, land };
}
async function runPreview() {
  if (!editSample) return;
  if (previewCtrl) previewCtrl.abort();
  const ctrl = previewCtrl = new AbortController();
  // zoomed in: render every panel pixel (~1 s) instead of the half-size draft (~0.3 s)
  const full = dZoom.zoomed;
  $("#dprev-stage").classList.add("busy");
  try {
    const { blobUrl, faceBboxes } = await ditherPreview(editSample.name, editCfg, undefined, ctrl.signal, full ? DITHER_FULL_SIDE : DITHER_DRAFT_SIDE);
    const { url, box, land } = await trimLetterbox(blobUrl);
    if (ctrl.signal.aborted) { URL.revokeObjectURL(url); return; }   // superseded while trimming
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = url; previewFull = full; previewGeom = { box, land };
    const img = $("#dprev-img"); img.classList.remove("broken"); img.src = url;
    dZoom.setReady(true);
    const layer = $("#dprev-faces");
    if (faceBboxes && faceBboxes.length) {
      // face boxes are normalised to the untrimmed render; re-normalise them to the trimmed one
      const [bx, by, bw, bh] = box;
      layer.hidden = false;
      layer.innerHTML = faceBboxes.map((f) => `<i style="left:${(f[0] - bx) / bw * 100}%;top:${(f[1] - by) / bh * 100}%;width:${f[2] / bw * 100}%;height:${f[3] / bh * 100}%"></i>`).join("");
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
  dZoom.setReady(false);   // Fit, controls hidden until the first render arrives
  ditherModal.hidden = false;
  if (editSample) runPreview(); else toast("Upload a photo to preview the dither.");
}
function closeDither() { ditherModal.hidden = true; clearTimeout(previewTimer); if (previewCtrl) previewCtrl.abort(); dZoom.reset(); }
ditherModal.addEventListener("click", (e) => { if (e.target === ditherModal || e.target.closest("[data-dither-close]")) closeDither(); });
$("#dcats").addEventListener("click", (e) => { const b = e.target.closest("[data-stage]"); if (!b) return; ditherStage = +b.dataset.stage; renderDitherCats(); renderDitherControls(); });
$("#dprev-pick").addEventListener("click", (e) => { const b = e.target.closest("[data-sample]"); if (!b) return; editSample = ditherPhotos().find((t) => t.name === b.dataset.sample); renderDitherPicker(); dZoom.reset(); runPreview(); });
// Fit ↔ 100% (button, Z, double-click; drag pans). 100% wants every panel pixel, so zooming in
// fetches the full-size render; the current one shows enlarged until it arrives. Zoom and
// position stay put while sliders re-render, so the same patch of dots can be watched.
const dZoom = stageZoom({
  stage: $("#dprev-stage"), wrap: $("#dprev-stage .dprev-wrap"), img: $("#dprev-img"),
  panelPx: () => (previewGeom ? (previewGeom.land ? 1600 : 1200) * previewGeom.box[2] : 0),
  isOpen: () => !ditherModal.hidden,
  onZoom: (zoomed) => { if (zoomed && !previewFull) runPreview(); },
});
$("#dither-reset").addEventListener("click", () => { editCfg = clone(state.config.config[editPipeline]); renderDitherControls(); runPreview(); toast("Reset to saved pipeline"); });
$("#dither-save").addEventListener("click", () => {
  draft[editPipeline] = clone(editCfg);
  closeDither();
  renderTab(activeTab);   // reflect "Custom" in the preset dropdown
  toast("Dither applied — press Save to keep it");
});

// ══ E1003 mono tone editor — the colour dither editor's live-preview model, tailored
// to the flat mono knobs. The preview is the real 16-level panel render (grayscale =
// the unmissable "you're editing the mono look" signal). Same desktop-only rule. ══
const monoModal = $("#mono-modal");
let monoCfg = null, monoSample = null, monoPrevTimer = null, monoPrevCtrl = null, monoPrevKey = null;
let monoFull = false;   // whether the shown render is the panel's native size (for 100% zoom)
const MONO_PANEL_LONG = 1872, MONO_PANEL_SHORT = 1404;   // the E1003 panel (mono_e1003.MONO_W x MONO_H)
// press-and-hold "before" peek: monoBaseCfg is the config the editor OPENED with
// (your pre-edit baseline); its render is stacked over the live preview and shown
// only while held. A quick tap must not flash it.
let monoBaseCfg = null, monoBeforeCtrl = null;
// The live and "before" renders, decoded, and the blob URLs of their trimmed cut-outs. Both
// are cut to ONE shared box (monoBox) so hold-to-compare lines up dot for dot. The box comes
// from the pre-edit render once it's in, since a tone edit can turn a photo edge pure white
// (which a per-render trim would eat, resizing the photo mid-drag); until then, the live one.
const monoShots = { live: { bmp: null, url: null }, before: { bmp: null, url: null } };
let monoBox = null;   // { box, land, base } — base: it came from the pre-edit render
const MO_PEEK_HOLD_MS = 280, MO_PEEK_MOVE_TOL = 12;
let moPeekTimer = null, moPeekAt = null, moPeeking = false;
// ── Custom editor: control specs across 3 left-menu pages. Values are stored in the
// engine's own units (basic tonals in [-1,1], exposure in EV, sigmoid/lc/clahe raw);
// the fmt fns render them Lightroom/darktable-style. Defaults = the B&W Contrast
// starting point (Basic neutral) so a fresh Custom equals the fixed preset. ──
const moLR = (v) => (v > 0 ? "+" : "") + Math.round(v * 100);          // basic tonal: -1..1 shown ±100
const moEV = (v) => (v > 0 ? "+" : "") + (+v).toFixed(1) + " EV";      // exposure
const moP100 = (v) => Math.round(v * 100) + "%";                       // lc hi/sh: 0..2 -> %
const moDetail = (v) => Math.round(v * 100 + 100) + "%";              // lc detail: darktable +100 offset
const moPct = (v) => Math.round(v) + "%", moPx = (v) => (+v).toFixed(2) + " px", moInt = (v) => String(Math.round(v));
const mo2 = (v) => (+v).toFixed(2), mo3 = (v) => (+v).toFixed(3), mo2pct = (v) => (+v).toFixed(2) + "%", mo4pct = (v) => (+v).toFixed(4) + "%";
const moSk = (v) => (v > 0 ? "+" : "") + (+v).toFixed(2);
const MONO_PAGES = [
  { id: "tone", label: "Tone", hint: "Lightroom Basic", toggle: { key: "mono_e1003_basic_enabled", label: "Basic" }, knobs: [
    { key: "mono_e1003_basic_exposure", label: "Exposure", min: -5, max: 5, step: 1 / 3, def: 0, neutral: 0, fmt: moEV },
    { key: "mono_e1003_basic_contrast", label: "Contrast", min: -1, max: 1, step: 0.01, def: 0, neutral: 0, fmt: moLR },
    { key: "mono_e1003_basic_highlights", label: "Highlights", min: -1, max: 1, step: 0.01, def: 0, neutral: 0, fmt: moLR },
    { key: "mono_e1003_basic_shadows", label: "Shadows", min: -1, max: 1, step: 0.01, def: 0, neutral: 0, fmt: moLR },
    { key: "mono_e1003_basic_whites", label: "Whites", min: -1, max: 1, step: 0.01, def: 0, neutral: 0, fmt: moLR },
    { key: "mono_e1003_basic_blacks", label: "Blacks", min: -1, max: 1, step: 0.01, def: 0, neutral: 0, fmt: moLR },
    { key: "mono_e1003_basic_clahe", label: "Local contrast (CLAHE)", min: 0, max: 5, step: 0.25, def: 0, fmt: mo2 },
  ] },
  { id: "advanced", label: "Advanced Local Contrast", hint: "darktable sigmoid + local-laplacian", groups: [
    { toggle: { key: "mono_e1003_sig_enabled", label: "Sigmoid" }, knobs: [
      { key: "mono_e1003_sig_contrast", label: "Contrast", min: 0.7, max: 3, step: 0.001, def: 0.735, neutral: 1.5, fmt: mo3 },
      { key: "mono_e1003_sig_skew", label: "Skew", min: -1, max: 1, step: 0.01, def: 1.0, neutral: 0, fmt: moSk },
      { key: "mono_e1003_sig_white", label: "Target white", min: 50, max: 100, step: 0.1, def: 100, neutral: 100, fmt: mo2pct },
      { key: "mono_e1003_sig_black", label: "Target black", min: 0, max: 5, step: 0.0001, def: 0.7634, neutral: 0, fmt: mo4pct },
    ] },
    { toggle: { key: "mono_e1003_lc_enabled", label: "Local contrast" }, knobs: [
      { key: "mono_e1003_lc_detail", label: "Detail", min: -1, max: 4, step: 0.01, def: 1.39, neutral: 0.25, fmt: moDetail }, /* dt default 125% -> 0.25 */
      { key: "mono_e1003_lc_highlights", label: "Highlights", min: 0, max: 2, step: 0.01, def: 0.5, neutral: 0.5, fmt: moP100 },
      { key: "mono_e1003_lc_shadows", label: "Shadows", min: 0, max: 2, step: 0.01, def: 0.5, neutral: 0.5, fmt: moP100 },
      { key: "mono_e1003_lc_midtone", label: "Midtone range", min: 0.001, max: 1, step: 0.005, def: 0.5, neutral: 0.5, fmt: mo3 },
    ] },
  ] },
  { id: "details", label: "Details", hint: "Pre-dither sharpening", knobs: [
    { key: "mono_e1003_sharpen_amount", label: "Sharpening", min: 0, max: 300, step: 5, def: 170, fmt: moPct },
    { key: "mono_e1003_sharpen_radius", label: "Radius", min: 0.2, max: 3, step: 0.05, def: 1.2, fmt: moPx },
    { key: "mono_e1003_sharpen_threshold", label: "Threshold", min: 0, max: 10, step: 1, def: 2, fmt: moInt },
  ] },
];
const moKnobsOf = (p) => (p.knobs || []).concat(...(p.groups || []).map((g) => g.knobs));
const MONO_KNOB_BY_KEY = Object.fromEntries(MONO_PAGES.flatMap(moKnobsOf).map((k) => [k.key, k]));
let monoPage = "tone";
function moKnobHTML(spec) {
  const v = monoCfg[spec.key], tweaked = Math.abs(v - spec.def) > 1e-9;
  // Detent marks each slider's HOME reference, not the geometric centre: the
  // darktable factory default for the sigmoid/local-contrast sliders (contrast 1.5,
  // detail 125%, ...), and the neutral 0 for the Lightroom Basic sliders. Rarely 50%.
  // Omit the tick where no meaningful home exists (CLAHE, sharpening).
  const hasDet = spec.neutral !== undefined;
  const detPct = hasDet ? ((spec.neutral - spec.min) / (spec.max - spec.min)) * 100 : 0;
  return `<div class="dknob${tweaked ? " tweaked" : ""}"><div class="dknob-head"><span class="dknob-lab">${spec.label}</span><span class="dknob-val" data-val="${spec.key}">${spec.fmt(v)}</span></div>` +
    `<div class="dtrack${hasDet ? "" : " no-detent"}"${hasDet ? ` style="--det:${detPct.toFixed(2)}%"` : ""}><input type="range" class="dslider" data-knob="${spec.key}" min="${spec.min}" max="${spec.max}" step="${spec.step}" value="${v}"></div></div>`;
}
const moToggleHTML = (tg) => `<label class="mono-toggle"><input type="checkbox" data-toggle="${tg.key}"${monoCfg[tg.key] ? " checked" : ""}><span>${tg.label}</span></label>`;
function moPageHTML(page) {
  if (page.groups) return page.groups.map((g) => `<div class="mono-group">${moToggleHTML(g.toggle)}${g.knobs.map(moKnobHTML).join("")}</div>`).join("");
  return (page.toggle ? `<div class="mono-group">${moToggleHTML(page.toggle)}${(page.knobs || []).map(moKnobHTML).join("")}</div>` : (page.knobs || []).map(moKnobHTML).join(""));
}
function renderMonoControls() {
  const ctrl = $("#mocontrols");
  const page = MONO_PAGES.find((p) => p.id === monoPage) || MONO_PAGES[0];
  ctrl.innerHTML =
    '<div class="mono-menu">' + MONO_PAGES.map((p) => `<button class="mono-mbtn${p.id === monoPage ? " on" : ""}" data-page="${p.id}"><span>${p.label}</span><small>${p.hint}</small></button>`).join("") + "</div>" +
    `<div class="mono-page">${moPageHTML(page)}</div>`;
  ctrl.querySelectorAll(".mono-mbtn").forEach((b) => b.addEventListener("click", () => { monoPage = b.dataset.page; renderMonoControls(); }));
  ctrl.querySelectorAll("[data-toggle]").forEach((c) => c.addEventListener("change", () => { monoCfg[c.dataset.toggle] = c.checked; scheduleMonoPreview(); }));
  ctrl.querySelectorAll(".dslider[data-knob]").forEach((sl) => {
    const spec = MONO_KNOB_BY_KEY[sl.dataset.knob];
    const apply = (v) => {
      monoCfg[spec.key] = v; sl.value = v;
      ctrl.querySelector(`[data-val="${spec.key}"]`).textContent = spec.fmt(v);
      sl.closest(".dknob").classList.toggle("tweaked", Math.abs(v - spec.def) > 1e-9);
      scheduleMonoPreview();
    };
    // Magnetic detent: while dragging, pull the value to the slider's home (spec.neutral)
    // when it lands within 2% of the track — so it visibly snaps at the tick mark.
    const snap = (v) => {
      if (spec.neutral === undefined) return v;
      const band = (spec.max - spec.min) * 0.02;
      return Math.abs(v - spec.neutral) <= band ? spec.neutral : v;
    };
    sl.addEventListener("input", () => apply(snap(+sl.value)));
    sl.addEventListener("dblclick", () => apply(spec.def));   // double-click a slider to reset it to default
  });
}
// map the mono_e1003_* draft keys to the short flat keys the preview endpoint reads
// (profile "custom" -> the backend builds the dtcore dict from these via _build_dtcore)
const monoPayload = (cfg = monoCfg) => ({
  profile: "custom",
  sharpen_amount: +cfg.mono_e1003_sharpen_amount,
  sharpen_radius: +cfg.mono_e1003_sharpen_radius,
  sharpen_threshold: +cfg.mono_e1003_sharpen_threshold,
  sig_enabled: !!cfg.mono_e1003_sig_enabled, sig_contrast: +cfg.mono_e1003_sig_contrast, sig_skew: +cfg.mono_e1003_sig_skew, sig_white: +cfg.mono_e1003_sig_white, sig_black: +cfg.mono_e1003_sig_black,
  lc_enabled: !!cfg.mono_e1003_lc_enabled, lc_detail: +cfg.mono_e1003_lc_detail, lc_highlights: +cfg.mono_e1003_lc_highlights, lc_shadows: +cfg.mono_e1003_lc_shadows, lc_midtone: +cfg.mono_e1003_lc_midtone,
  basic_enabled: !!cfg.mono_e1003_basic_enabled, basic_exposure: +cfg.mono_e1003_basic_exposure, basic_contrast: +cfg.mono_e1003_basic_contrast, basic_highlights: +cfg.mono_e1003_basic_highlights, basic_shadows: +cfg.mono_e1003_basic_shadows, basic_whites: +cfg.mono_e1003_basic_whites, basic_blacks: +cfg.mono_e1003_basic_blacks, basic_clahe: +cfg.mono_e1003_basic_clahe,
});
function renderMonoPicker() {
  const pics = ditherPhotos();
  $("#moprev-pick").innerHTML = pics.map((t) => `<button data-sample="${esc(t.name)}"${monoSample && monoSample.name === t.name ? ' class="on"' : ""} title="${esc(t.name)}"><img alt="" src="${thumbnailUrl(t)}"></button>`).join("");
}
function scheduleMonoPreview() { clearTimeout(monoPrevTimer); monoPrevTimer = setTimeout(runMonoPreview, 400); }
// Like the dither editor, the tone editor shows the WHOLE photo in its own shape: rendered on a
// canvas of the photo's orientation and never zoomed to fill (the thin bars are trimmed into the
// mat). Framing is the frame's business, not the look's. full: the panel's native size (100%).
const monoOpts = (full) => ({
  frame_portrait: (monoSample?.native_orientation || monoSample?.effective_orientation) === "portrait",
  fill: false,
  ...(full ? { maxSidePx: MONO_PANEL_LONG } : {}),
});
// Take a fresh render (blob URL) for the live or "before" slot: decode it, settle the shared
// trim box, and show its cut-out in the white mat. isCurrent() is false once a newer request
// has replaced this one; a superseded render is dropped. Returns whether it was shown.
async function monoShow(which, blobUrl, isCurrent) {
  const im = new Image(); im.src = blobUrl;
  let bmp;
  try { await im.decode(); bmp = await createImageBitmap(im); } finally { URL.revokeObjectURL(blobUrl); }
  if (!isCurrent()) { bmp.close(); return false; }
  const s = monoShots[which];
  if (s.bmp) s.bmp.close();
  s.bmp = bmp;
  if (!monoBox || (which === "before" && !monoBox.base)) {
    const found = letterboxBox(bmp);
    const same = monoBox && found.box.every((v, i) => v === monoBox.box[i]);
    monoBox = { ...found, base: which === "before" };
    if (!same && which === "before") monoCut("live");   // re-cut the live render to the shared box
  }
  await monoCut(which);
  return true;
}
async function monoCut(which) {
  const s = monoShots[which], bmp = s.bmp;
  if (!bmp || !monoBox) return;
  const url = await cropToBox(bmp, monoBox.box);
  if (s.bmp !== bmp) { URL.revokeObjectURL(url); return; }   // superseded while encoding
  if (s.url) URL.revokeObjectURL(s.url);
  s.url = url;
  $(which === "live" ? "#moprev-img" : "#moprev-before").src = url;
}
// drop both renders and the shared box (the editor is opening afresh)
function monoForget() {
  for (const s of Object.values(monoShots)) { if (s.bmp) s.bmp.close(); if (s.url) URL.revokeObjectURL(s.url); s.bmp = s.url = null; }
  monoBox = null;
}
// a new sample frames differently: forget the decoded renders and the box, but leave the
// shown cut-outs up until the new sample's renders replace them
function monoNewSample() {
  for (const s of Object.values(monoShots)) { if (s.bmp) s.bmp.close(); s.bmp = null; }
  monoBox = null;
}
async function runMonoPreview() {
  if (!monoSample) return;
  // Dedupe: skip the round-trip when the effective payload is unchanged (detent snaps,
  // dbl-click resets to the current value, re-opening the same sample). Keyed on the
  // sample name + the exact payload we'd send.
  const payload = monoPayload();
  const full = mZoom.zoomed;   // zoomed in: the panel's native size instead of the 900-px draft
  const key = monoSample.name + "|" + full + "|" + JSON.stringify(payload);
  if (key === monoPrevKey && monoShots.live.url) return;
  if (monoPrevCtrl) monoPrevCtrl.abort();
  const ctrl = monoPrevCtrl = new AbortController();
  $("#moprev-stage").classList.add("busy");
  try {
    const url = await monoPreview(monoSample.name, payload, ctrl.signal, monoOpts(full));
    if (!(await monoShow("live", url, () => ctrl === monoPrevCtrl && !ctrl.signal.aborted))) return;
    monoPrevKey = key; monoFull = full;
    $("#moprev-img").classList.remove("broken");
    mZoom.setReady(true);
    $("#moprev-stage").classList.remove("busy");
  } catch (e) {
    if (e.name === "AbortError") return;   // superseded by a newer request
    $("#moprev-stage").classList.remove("busy");
    $("#moprev-img").classList.add("broken");
    toast("Preview failed: " + e.message);
  }
}
// render the pre-edit baseline for the current sample into the stacked "before" img
async function renderMonoBefore() {
  if (!monoBaseCfg || !monoSample) return;
  if (monoBeforeCtrl) monoBeforeCtrl.abort();
  const ctrl = monoBeforeCtrl = new AbortController();
  try {
    // same size as the live render, so hold-to-compare lines up dot for dot at 100%
    const url = await monoPreview(monoSample.name, monoPayload(monoBaseCfg), ctrl.signal, monoOpts(mZoom.zoomed));
    await monoShow("before", url, () => ctrl === monoBeforeCtrl && !ctrl.signal.aborted);
  } catch (e) { if (e.name !== "AbortError") $("#moprev-before").removeAttribute("src"); }
}
function endMonoPeek() {
  clearTimeout(moPeekTimer); moPeekTimer = null; moPeekAt = null;
  if (moPeeking) { moPeeking = false; monoModal.querySelector(".dprev-wrap").classList.remove("peek"); }
}
function openMono() {
  if (isMobileVp()) { toast("The mono tone editor is desktop-only."); return; }
  monoCfg = {}; MONO_KEYS.forEach((k) => (monoCfg[k] = clone(draft[k])));
  monoCfg.mono_e1003_profile = "custom";   // opening the editor = editing the Custom profile
  monoPage = "tone";                        // always land on the first page
  monoBaseCfg = clone(monoCfg);   // snapshot the pre-edit baseline for the hold-to-compare peek
  monoSample = ditherPhotos()[0] || null;
  renderMonoControls(); renderMonoPicker();
  $("#moprev-img").removeAttribute("src"); $("#moprev-before").removeAttribute("src");
  monoForget(); monoPrevKey = null;   // cleared img -> force a fresh render even if the payload repeats
  mZoom.setReady(false);   // Fit, controls hidden until the first render arrives
  monoModal.hidden = false;
  if (monoSample) { runMonoPreview(); renderMonoBefore(); } else toast("Upload a photo to preview the mono look.");
}
function closeMono() {
  endMonoPeek();
  monoModal.hidden = true;
  clearTimeout(monoPrevTimer); if (monoPrevCtrl) monoPrevCtrl.abort(); if (monoBeforeCtrl) monoBeforeCtrl.abort();
  mZoom.reset();
}
monoModal.addEventListener("click", (e) => { if (e.target === monoModal || e.target.closest("[data-mono-close]")) closeMono(); });
$("#moprev-pick").addEventListener("click", (e) => { const b = e.target.closest("[data-sample]"); if (!b) return; monoSample = ditherPhotos().find((t) => t.name === b.dataset.sample); renderMonoPicker(); monoNewSample(); mZoom.reset(); runMonoPreview(); renderMonoBefore(); });
// press-and-hold the preview to peek at the pre-edit baseline (a quick tap never flashes it)
const moStage = $("#moprev-stage");
moStage.addEventListener("pointerdown", (e) => {
  if (!monoShots.live.url || !monoShots.before.url || $("#moprev-img").classList.contains("broken")) return;
  moPeekAt = { x: e.clientX, y: e.clientY };
  try { moStage.setPointerCapture(e.pointerId); } catch (_) {}
  clearTimeout(moPeekTimer);
  moPeekTimer = setTimeout(() => { moPeeking = true; monoModal.querySelector(".dprev-wrap").classList.add("peek"); }, MO_PEEK_HOLD_MS);
});
moStage.addEventListener("pointermove", (e) => {
  if (!moPeekAt || moPeeking) return;
  if (Math.hypot(e.clientX - moPeekAt.x, e.clientY - moPeekAt.y) > MO_PEEK_MOVE_TOL) endMonoPeek();
});
["pointerup", "pointercancel", "pointerleave"].forEach((ev) => moStage.addEventListener(ev, endMonoPeek));
moStage.addEventListener("contextmenu", (e) => e.preventDefault());
moStage.addEventListener("dragstart", (e) => e.preventDefault());
// Fit ↔ 100% for the tone editor (button, Z, double-click; drag pans), sharing the press rules
// above: holding still still compares, a drag pans instead. Zooming in fetches the live and
// "before" renders at the panel's native size.
const mZoom = stageZoom({
  stage: moStage, wrap: $("#moprev-stage .dprev-wrap"), img: $("#moprev-img"),
  panelPx: () => (monoBox ? (monoBox.land ? MONO_PANEL_LONG : MONO_PANEL_SHORT) * monoBox.box[2] : 0),
  isOpen: () => !monoModal.hidden,
  isPeeking: () => moPeeking,
  onZoom: (zoomed) => { if (zoomed && !monoFull) { runMonoPreview(); renderMonoBefore(); } },
});
$("#mono-reset").addEventListener("click", () => { MONO_KEYS.forEach((k) => (monoCfg[k] = clone(state.config.config[k]))); monoCfg.mono_e1003_profile = "custom"; renderMonoControls(); runMonoPreview(); toast("Reset to saved"); });
// Reset to defaults: Custom's settings back to the shipped defaults. This is the only reset
// for the E1003 look (the settings page's Reset leaves it alone). It changes the editor's
// working copy only — nothing is stored until "Use these settings" and then Save.
$("#mono-defaults").addEventListener("click", (e) => armConfirm(e.currentTarget, () => {
  const defs = state.config?.config_defaults; if (!defs) return;
  MONO_KEYS.forEach((k) => { if (k !== "mono_e1003_profile" && k in defs) monoCfg[k] = clone(defs[k]); });
  renderMonoControls(); runMonoPreview();
  toast("Custom reset to defaults — Use these settings to apply");
}, "Reset to defaults?"));
$("#mono-save").addEventListener("click", () => {
  MONO_KEYS.forEach((k) => (draft[k] = clone(monoCfg[k])));
  closeMono();
  renderTab(activeTab);   // reflect Custom / matched preset in the dropdown
  toast("Mono tone applied — press Save to keep it");
});

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (!monoModal.hidden) closeMono(); else if (!ditherModal.hidden) closeDither(); else closeSettings(); } });
