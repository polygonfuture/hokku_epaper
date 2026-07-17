// api.js — ALL server I/O lives here. Absolute paths so the page location is
// irrelevant; every caller gets parsed JSON or a thrown Error with the server's
// message. Image URLs are cache-busted here and nowhere else.

const API = "/hokku/api";

async function req(path, opts = {}) {
  const res = await fetch(API + path, opts);
  let body = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) body = await res.json();
  if (!res.ok) {
    const msg = body && body.error ? body.error : `${res.status} ${res.statusText}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

const json = (method, data) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data),
});

// ── reads ──
export const getStatus = () => req("/status");
export const getConfig = () => req("/config");

// ── config ──
export const postConfig = (partial) => req("/config", json("POST", partial));

// ── image actions ──
export const deleteImage = (name) => req(`/image/${encodeURIComponent(name)}`, { method: "DELETE" });
export const retryImage = (name) => req(`/image/${encodeURIComponent(name)}/retry`, { method: "POST" });
export const showNext = (name) => req(`/show_next/${encodeURIComponent(name)}`, { method: "POST" });
export const clearCache = () => req("/clear_cache", { method: "POST" });
export const clearClassifier = () => req("/classifier/clear", { method: "POST" });
export const scrub = () => req("/scrub", { method: "POST" });

// ── screens ──
export const deleteScreen = (name) => req(`/screens/${encodeURIComponent(name)}`, { method: "DELETE" });
export const patchScreen = (name, cfg) => req(`/screens/${encodeURIComponent(name)}/config`, json("PATCH", cfg));
// per-screen show-next override (backend feature added in this project — M1)
export const screenShowNext = (screen, image) =>
  req(`/screens/${encodeURIComponent(screen)}/show_next`, json("POST", { image }));
export const clearScreenShowNext = (screen) =>
  req(`/screens/${encodeURIComponent(screen)}/show_next`, { method: "DELETE" });

// ── image URLs (cache-busted) ──
// /thumbnail and /dithered send no cache validators and are name-keyed, so the key
// must change exactly when the render changes: size_bytes + last_conversion_seconds.
export function bust(entry) {
  return `v=${entry.size_bytes ?? 0}-${entry.last_conversion_seconds ?? 0}`;
}
export const thumbnailUrl = (entry) => `${API}/thumbnail/${encodeURIComponent(entry.name)}?${bust(entry)}`;
export const ditheredUrl = (entry) => `${API}/dithered/${encodeURIComponent(entry.name)}?${bust(entry)}`;
export const originalUrl = (entry) => `${API}/original/${encodeURIComponent(entry.name)}`;

// ── upload (XHR for real progress events) ──
// onProgress(fraction 0..1). Resolves with {saved:[], skipped:[{name,reason}]}.
export function upload(files, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/upload`);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener("load", () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* non-JSON error page */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body && body.error ? body.error : `upload failed (${xhr.status})`));
    });
    xhr.addEventListener("error", () => reject(new Error("upload failed (network)")));
    xhr.send(fd);
  });
}

// ── dither preview (settings' custom editor; desktop only) ──
// Returns {blobUrl, faceBboxes} — caller must URL.revokeObjectURL(blobUrl) when done.
export async function ditherPreview(name, imageConfig, claheKeepout, signal) {
  const res = await fetch(`${API}/dither/preview`, {
    ...json("POST", { name, image: imageConfig, ...(claheKeepout === undefined ? {} : { clahe_keepout: claheKeepout }) }),
    signal,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b.error) msg = b.error; } catch { /* png or empty */ }
    throw new Error(msg);
  }
  const faceBboxes = JSON.parse(res.headers.get("X-Face-Bboxes") || "[]");
  const blobUrl = URL.createObjectURL(await res.blob());
  return { blobUrl, faceBboxes };
}
