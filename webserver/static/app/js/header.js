// header.js — the Lightroom-style activity center (converting / upload / failed /
// debug tiles + their dropdown), real XHR uploads with drag-drop, and the failed-
// conversions modal. Ported from the mockup's status-center + upload + failed-modal
// code; the demo tickers are replaced by state.status (converting_*, failed_files,
// debug_fast_refresh) and the fake upload by api.upload() with real progress events.

import { state, subscribe, mutate } from "./state.js";
import { upload, retryImage, deleteImage, thumbnailUrl } from "./api.js";
import { $, $$, esc, toast, fmtBytes, fmtEta, isMobileVp, armConfirm, disarm } from "./ui.js";
import { uploadAndEdit } from "./editor.js";

const statusTiles = $("#status-tiles");
const statusPop = $("#status-pop");

// ── status model (kind → {icon, spin, color, label, sub, detail, pct, action, dismiss}) ──
const ST_TILE_ORDER = ["upload", "convert", "failed", "debug"];
const ST_COLOR = { debug: "#E9836F", convert: "var(--accent)", upload: "#63A6E6", failed: "#E3A94F" };
const STATUS = new Map();
const stIcon = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">${inner}</svg>`;
const ST_IC = {
  alert: stIcon('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4.5M12 17h.01"/>'),
  spin:  stIcon('<path d="M21 12a9 9 0 1 1-6.2-8.6"/>'),
  up:    stIcon('<path d="M12 19V6M6 11l6-6 6 6"/>'),
  check: stIcon('<path d="M20 6L9 17l-5-5"/>'),
};

let popKind = null;
// failed folds into the convert tile while a conversion runs (own tile otherwise);
// on mobile, upload folds into the convert tile too (one activity chip).
const tileKinds = () => ST_TILE_ORDER.filter((k) =>
  STATUS.has(k) &&
  !(k === "failed" && STATUS.has("convert")) &&
  !(k === "upload" && STATUS.has("convert") && isMobileVp()));
const popRowsFor = (k) => k === "convert"
  ? ["upload", "convert", "failed"].filter((x) => STATUS.has(x) && (x !== "upload" || isMobileVp()))
  : [k];

// ── keyed tile rendering: reuse tile nodes so the spinner never restarts and the
//    ETA/percent update in place (no per-poll flicker) ──
function tileInnerHTML(k, s) {
  return `<span class="st-ic${s.spin ? " spin" : ""}" style="color:${s.color || ST_COLOR[k]}">${s.icon}</span>` +
    `<span class="st-lab">${esc(s.label)}</span>` +
    (s.sub ? `<span class="st-sub">${esc(s.sub)}</span>` : "") +
    (k === "convert" && STATUS.has("failed") ? `<span class="st-dots"><i style="background:${ST_COLOR.failed}"></i></span>` : "") +
    (s.pct != null ? `<span class="st-bar"><i style="width:${s.pct}%"></i></span>` : "");
}
function updateTile(el, k, s) {
  // structural signature — a change here rebuilds innerHTML; otherwise text/width
  // update in place (keeps the CSS spinner animation from restarting each poll)
  const sig = [k, s.spin ? 1 : 0, s.color || "", s.icon, s.sub != null, s.pct != null, k === "convert" && STATUS.has("failed")].join("|");
  if (el._sig !== sig) { el.innerHTML = tileInnerHTML(k, s); el._sig = sig; return; }
  el.querySelector(".st-lab").textContent = s.label;
  const sub = el.querySelector(".st-sub"); if (sub) sub.textContent = s.sub || "";
  const bar = el.querySelector(".st-bar i"); if (bar && s.pct != null) bar.style.width = s.pct + "%";
}
function renderStatus() {
  const kinds = tileKinds();
  [...statusTiles.children].forEach((el) => { if (!kinds.includes(el.dataset.skind)) el.remove(); });
  kinds.forEach((k, i) => {
    let el = statusTiles.querySelector(`:scope > [data-skind="${k}"]`);
    if (!el) { el = document.createElement("button"); el.className = "status-tile"; el.dataset.skind = k; }
    updateTile(el, k, STATUS.get(k));
    if (statusTiles.children[i] !== el) statusTiles.insertBefore(el, statusTiles.children[i] || null);   // reorder only when needed
  });
  if (popKind && !kinds.includes(popKind)) closeStatusPop();
  else if (!statusPop.hidden) renderStatusPop();
}
function renderStatusPop() {
  statusPop.innerHTML = popRowsFor(popKind).filter((k) => STATUS.has(k)).map((k) => {
    const s = STATUS.get(k);
    return `<div class="st-row" data-skind="${k}">` +
      `<span class="st-ic${s.spin ? " spin" : ""}" style="color:${s.color || ST_COLOR[k]}">${s.icon}</span>` +
      `<div class="st-txt"><b>${esc(s.label)}</b>` +
        (s.detail || s.sub ? `<span>${esc(s.detail || s.sub)}</span>` : "") +
        (s.pct != null ? `<span class="st-rowbar"><i style="width:${s.pct}%"></i></span>` : "") + "</div>" +
      (s.action ? `<button class="st-act" data-staction="${s.action.act}">${esc(s.action.label)}</button>` : "") +
      (s.dismiss ? '<button class="st-x" data-stx="' + k + '" aria-label="Dismiss"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' : "") +
      "</div>";
  }).join("");
}
function setStatus(kind, s) { STATUS.set(kind, s); renderStatus(); }
function clearStatus(kind) { if (STATUS.delete(kind)) renderStatus(); }
function openStatusPop(kind, anchor) {
  popKind = kind; renderStatusPop(); statusPop.hidden = false;
  const r = anchor.getBoundingClientRect();
  statusPop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 344)) + "px";
  statusPop.style.top = (r.bottom + 8) + "px";
}
function closeStatusPop() { statusPop.hidden = true; popKind = null; }

statusTiles.addEventListener("click", (e) => {
  const t = e.target.closest("[data-skind]"); if (!t) return;
  e.stopPropagation();
  (popKind === t.dataset.skind && !statusPop.hidden) ? closeStatusPop() : openStatusPop(t.dataset.skind, t);
});
document.addEventListener("click", (e) => {
  if (!statusPop.hidden && !e.target.closest("#status-pop") && !e.target.closest("#status-tiles")) closeStatusPop();
});
statusPop.addEventListener("click", (e) => {
  const x = e.target.closest("[data-stx]"); if (x) { clearStatus(x.dataset.stx); return; }
  const a = e.target.closest("[data-staction]");
  if (a && a.dataset.staction === "review-failed") { closeStatusPop(); openFailedModal(); }
});

// ── tiles driven by the live status/config (upload tile is driven by the XHR flow) ──
function syncTiles() {
  const st = state.status, cfg = state.config;
  // converting — show while an active batch has items left; label counts the in-flight one
  if (st && st.converting_total > 0 && st.converting_done < st.converting_total) {
    const done = st.converting_done, total = st.converting_total;
    setStatus("convert", {
      icon: ST_IC.spin, spin: true,
      label: `Dithering ${Math.min(done + 1, total)} of ${total}`,
      sub: fmtEta(st.converting_eta_seconds),
      detail: st.converting_name ? `Converting ${st.converting_name}` : `${total - done} left`,
      pct: Math.round(done / total * 100),
    });
  } else clearStatus("convert");
  // failed summary
  const nf = st ? (st.failed_files || []).length : 0;
  if (nf) setStatus("failed", {
    icon: ST_IC.alert, label: `${nf} failed`,
    detail: `${nf} photo${nf > 1 ? "s" : ""} failed to convert — review and retry, or delete.`,
    action: { act: "review-failed", label: "Review" },
  });
  else { clearStatus("failed"); if (!failedModal.hidden) closeFailedModal(); }
  // debug fast-refresh warning
  if (cfg?.config?.debug_fast_refresh) setStatus("debug", {
    icon: ST_IC.alert, label: "Debug active",
    detail: "Frames are refreshing very frequently — this drains their batteries. Turn Debug off in Server settings.",
  });
  else clearStatus("debug");
  if (!failedModal.hidden) renderFailedModal();
}

// ══ Upload: native picker + desktop drag-drop → real multipart XHR ══
const fileInput = $("#file-input");
$("#upload-btn").addEventListener("click", () => fileInput.click());
// one file → upload as a draft and open the editor; several → straight into conversion
fileInput.addEventListener("change", () => {
  const files = [...fileInput.files];
  fileInput.value = "";
  if (files.length === 1) uploadAndEdit(files[0]);
  else if (files.length) doUpload(files);
});

async function doUpload(files) {
  const n = files.length;
  setStatus("upload", { icon: ST_IC.up, color: ST_COLOR.upload, label: `Uploading ${n} photo${n > 1 ? "s" : ""}…`, sub: "0%", pct: 0 });
  try {
    const res = await upload(files, (frac) => {
      const pct = Math.round(frac * 100);
      setStatus("upload", { icon: ST_IC.up, color: ST_COLOR.upload, label: `Uploading ${n} photo${n > 1 ? "s" : ""}…`, sub: pct + "%", pct });
    });
    const saved = res?.saved || [], skipped = res?.skipped || [];
    setStatus("upload", {
      icon: ST_IC.check, color: "#82C08C",
      label: `${saved.length} uploaded`,
      detail: skipped.length ? `${skipped.length} skipped` : "Queued for conversion",
      dismiss: true,
    });
    if (skipped.length) {
      const names = skipped.slice(0, 2).map((s) => s.name).join(", ");
      toast(`${skipped.length} skipped: ${names}${skipped.length > 2 ? "…" : ""} (${skipped[0].reason})`);
    }
    mutate();   // pull the new images into the gallery + converting tile
    setTimeout(() => clearStatus("upload"), 2600);
  } catch (e) {
    clearStatus("upload");
    toast("Upload failed: " + e.message);
  }
}

// desktop drag-and-drop overlay
const overlay = $("#drop-overlay");
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; overlay.hidden = false; } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) overlay.hidden = true; });
window.addEventListener("drop", (e) => {
  if (hasFiles(e)) {
    e.preventDefault();
    const f = [...e.dataTransfer.files];
    if (f.length === 1) uploadAndEdit(f[0]); else if (f.length) doUpload(f);
  }
  dragDepth = 0; overlay.hidden = true;
});

// ══ Failed-conversions modal ══
const failedModal = $("#failed-modal");
const failedList = () => (state.status?.failed_files || []);
let failedSig = null;
function renderFailedModal(force) {
  const failed = failedList();
  $("#failed-count").textContent = failed.length;
  // only rebuild when the failed set changes — otherwise a poll would reset a
  // mid-confirm delete arm and needlessly reload the row thumbnails
  const sig = failed.map((f) => `${f.name}|${f.size_bytes}|${f.error || ""}`).join(";");
  if (!force && sig === failedSig) return;
  failedSig = sig;
  const uploads = state.status?.upload_files || [];
  $("#failed-body").innerHTML = '<div class="failed-list">' + failed.map((f) => {
    const entry = uploads.find((e) => e.name === f.name) || f;
    return `<div class="failed-row" data-name="${esc(f.name)}">` +
      `<img alt="" src="${thumbnailUrl(entry)}">` +
      `<div class="fr-info"><span class="fr-name">${esc(f.name)}</span><span class="fr-err">${esc(f.error || "Conversion failed")}</span></div>` +
      `<span class="fr-size">${fmtBytes(f.size_bytes)}</span>` +
      `<button class="chip-btn fr-retry" data-retry="${esc(f.name)}">Retry</button>` +
      `<button class="chip-btn danger fr-del" data-del="${esc(f.name)}"><span class="lbl">Delete</span></button>` +
      "</div>";
  }).join("") + "</div>";
  $$("#failed-body .failed-row img").forEach((img) => {
    img.addEventListener("error", () => img.classList.add("broken"));
  });
}
function openFailedModal() { renderFailedModal(true); failedModal.hidden = false; }
function closeFailedModal() { failedModal.hidden = true; disarm(anyArmedInFailed()); }
function anyArmedInFailed() { return failedModal.querySelector(".armed"); }

failedModal.addEventListener("click", (e) => {
  if (e.target === failedModal || e.target.closest("[data-failed-close]")) { closeFailedModal(); return; }
  const retryAll = e.target.closest('[data-failed="retry"]');
  const delAll = e.target.closest('[data-failed="delete"]');
  const rowRetry = e.target.closest("[data-retry]");
  const rowDel = e.target.closest("[data-del]");
  if (retryAll) {
    const names = failedList().map((f) => f.name);
    Promise.allSettled(names.map((n) => retryImage(n))).then(() => { mutate(); toast(`Re-converting ${names.length} photo${names.length > 1 ? "s" : ""}…`); });
  } else if (delAll) {
    armConfirm(delAll, () => {
      const names = failedList().map((f) => f.name);
      Promise.allSettled(names.map((n) => deleteImage(n))).then(() => {
        mutate((st) => { st.failed_files = []; st.upload_files = (st.upload_files || []).filter((e) => !names.includes(e.name)); });
        toast(`Deleted ${names.length} failed photo${names.length > 1 ? "s" : ""}`);
      });
    }, "Delete all?");
  } else if (rowRetry) {
    const name = rowRetry.dataset.retry;
    retryImage(name).then(() => { mutate(); toast(`Re-converting ${name}…`); }).catch((err) => toast("Retry failed: " + err.message));
  } else if (rowDel) {
    const name = rowDel.dataset.del;
    armConfirm(rowDel, () => {
      deleteImage(name).then(() => {
        mutate((st) => {
          st.failed_files = (st.failed_files || []).filter((f) => f.name !== name);
          st.upload_files = (st.upload_files || []).filter((e) => e.name !== name);
        });
        toast(`Deleted ${name}`);
      }).catch((err) => toast("Delete failed: " + err.message));
    }, "Confirm?");
  }
});

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeStatusPop(); if (!failedModal.hidden) closeFailedModal(); } });

// ── track the store ──
subscribe((what) => { if (what === "status" || what === "config") syncTiles(); });
if (state.status || state.config) syncTiles();
