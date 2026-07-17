// state.js — one tiny store. Views subscribe; the poll loop and mutations both
// funnel through here, so re-renders happen in exactly one place.

import { getStatus, getConfig } from "./api.js";

const S = {
  status: null,   // GET /hokku/api/status payload
  config: null,   // GET /hokku/api/config payload (config, config_defaults, dither_presets, panel, git_describe…)
  online: true,   // last poll succeeded
};

const subs = new Set();
let suppressUntil = 0;   // mutations set this so an in-flight poll can't repaint stale data over an optimistic update

export const state = S;
export function subscribe(fn) { subs.add(fn); return () => subs.delete(fn); }
function emit(what) { for (const fn of subs) fn(what, S); }

export async function refreshStatus() {
  try {
    const st = await getStatus();
    if (Date.now() < suppressUntil) return;   // a mutation just changed the world; wait for the next poll
    S.status = st;
    S.online = true;
    emit("status");
  } catch (e) {
    S.online = false;
    emit("offline");
  }
}

export async function refreshConfig() {
  S.config = await getConfig();
  emit("config");
}

// Call around any mutating API call: applies an optimistic patch immediately and
// keeps the next poll from clobbering it while the server settles.
export function mutate(patchFn) {
  if (patchFn && S.status) patchFn(S.status);
  suppressUntil = Date.now() + 1500;
  emit("status");
  // pull the real truth shortly after the suppression window
  setTimeout(refreshStatus, 1600);
}

// ── poll loop: 5s like the old UI, paused while the tab is hidden ──
let timer = null;
export function startPolling(intervalMs = 5000) {
  const tick = () => { if (!document.hidden) refreshStatus(); };
  if (timer) clearInterval(timer);
  timer = setInterval(tick, intervalMs);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
}
