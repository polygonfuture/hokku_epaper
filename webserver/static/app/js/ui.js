// ui.js — small dom + formatting helpers shared by every view.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── toast (bottom center, auto-hide; same element as the mockup) ──
export function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

// ── formatting ──
export function fmtBytes(n) {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + " MB";
  if (n >= 1e3) return Math.round(n / 1e3) + " KB";
  return n + " B";
}

// "2m ago" style relative time from an ISO string (server-local timestamps).
export function fmtAgo(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 50) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.round((s % 3600) / 60) + "m ago";
  return Math.floor(s / 86400) + "d ago";
}

// "in 1h 42m" style until an ISO timestamp; negative → "overdue"
export function fmtUntil(iso) {
  if (!iso) return "—";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "overdue";
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m";
  return Math.floor(s / 3600) + "h " + Math.round((s % 3600) / 60) + "m";
}

export function fmtEta(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))}s`;
  if (seconds < 3600) return `~${Math.round(seconds / 60)}m`;
  return `~${(seconds / 3600).toFixed(1)}h`;
}

export const fmtClock = (iso) => (iso ? iso.slice(11, 19) : "—");

// uptime seconds → "3d 4h 12m" / "6h 51m" / "44m" / "12s"
export function fmtUptime(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

// ── frame identity colors: stable per name (order-independent hash into a palette) ──
const FRAME_PALETTE = ["#E3A94F", "#82C08C", "#63A6E6", "#C08BD6", "#E9836F", "#5FC2BA", "#D6C08B"];
export function frameColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return FRAME_PALETTE[h % FRAME_PALETTE.length];
}

export const isMobileVp = () => !!(window.matchMedia && window.matchMedia("(max-width: 700px)").matches);

// ── two-step destructive confirm (Clear-cache pattern: text-only swap, no resize) ──
let armedBtn = null, armTimer = null;
export function disarm(btn) {
  if (!btn) return;
  clearTimeout(armTimer);
  btn._armed = false;
  btn.classList.remove("armed");
  const lbl = btn.querySelector(".lbl") || btn;
  if (btn._txt != null) { lbl.textContent = btn._txt; btn._txt = null; }
  if (armedBtn === btn) armedBtn = null;
}
export function armConfirm(btn, done, confirmText = "Confirm?") {
  if (btn._armed) { disarm(btn); done(); return; }
  if (armedBtn && armedBtn !== btn) disarm(armedBtn);
  const lbl = btn.querySelector(".lbl") || btn;
  btn._armed = true; armedBtn = btn; btn._txt = lbl.textContent;
  btn.classList.add("armed");
  lbl.textContent = confirmText;
  armTimer = setTimeout(() => disarm(btn), 3500);
}
export const anyArmed = () => armedBtn;
