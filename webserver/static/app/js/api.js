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
// per-frame HARD skip of the current "up next" — rerolls this frame only, returns {next_image}
export const skipScreenNext = (screen) =>
  req(`/screens/${encodeURIComponent(screen)}/skip`, { method: "POST" });

// ── image URLs (cache-busted) ──
// /thumbnail and /dithered send no cache validators and are name-keyed, so the key must
// change exactly when the render changes. render_version (a hash of the colour slugs + mono
// edit state) changes on EVERY edit; size_bytes/last_conversion_seconds did NOT (a crop edit
// leaves both unchanged → the browser reused a stale colour/mono preview). Fall back to the
// old key only if the server hasn't sent render_version yet.
export function bust(entry) {
  if (entry.render_version) return `v=${entry.render_version}`;
  return `v=${entry.size_bytes ?? 0}-${entry.last_conversion_seconds ?? 0}`;
}
export const thumbnailUrl = (entry) => `${API}/thumbnail/${encodeURIComponent(entry.name)}?${bust(entry)}`;
// the colour dither of the image. `orient` ("landscape"|"portrait") requests the render at
// a SPECIFIC frame orientation (the Frames drawer passes the frame's orientation so its card
// mirrors the panel); omit for the photo's own effective orientation (gallery/editor).
export const ditheredUrl = (entry, orient = null) =>
  `${API}/dithered/${encodeURIComponent(entry.name)}?${orient ? `orient=${orient}&` : ""}${bust(entry)}`;
// the E1003 (mono) render of the image — what a mono frame actually shows. `portrait`
// tells the server the frame's orientation so the render is composed for it. The PNG comes
// back upright and shaped to that orientation, exactly like ditheredUrl's — the E1003's
// landscape wire buffer is an internal detail the client never sees.
export const ditheredUrlMono = (entry, portrait = false) =>
  `${API}/dithered/${encodeURIComponent(entry.name)}?mono=1${portrait ? "&portrait=1" : ""}&${bust(entry)}`;
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

// Live E1003 (mono16) preview: renders the real wire pipeline and decodes through
// the panel's perceived ramp. `mono` is the flat knob set (see settings.js openMono).
// opts (optional): {rotation, crop:[x,y,w,h]} for the mono appearance's framing.
export async function monoPreview(name, mono, signal, opts = {}) {
  const body = { name, mono, max_side_px: 900 };
  if (opts.rotation) body.rotation = opts.rotation;
  if (opts.crop) body.crop = opts.crop;
  if (opts.frame_portrait) body.frame_portrait = true;   // E1003 frame orientation
  const res = await fetch(`${API}/dither/preview_mono`, {
    ...json("POST", body),
    signal,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b.error) msg = b.error; } catch { /* png or empty */ }
    throw new Error(msg);
  }
  return URL.createObjectURL(await res.blob());
}

// ── per-image editor (js/editor.js) ──
// The editor's opening state for an image: the classifier's own decision plus the
// source's orientation/dimensions and detection results.
export const getSuggestedConfig = (name) => req(`/image/${encodeURIComponent(name)}/suggested_config`);

// Like ditherPreview, but carries the editor's crop/rotation/target so the preview is
// pixel-exact for the edited framing. opts: {orientation, crop:[x,y,w,h], rotation, maxSidePx}.
export async function editorPreview(name, imageConfig, opts = {}, signal) {
  const body = { name, image: imageConfig };
  if (opts.orientation) body.orientation = opts.orientation;
  if (opts.crop) body.crop = opts.crop;
  if (opts.rotation) body.rotation = opts.rotation;
  if (opts.maxSidePx) body.max_side_px = opts.maxSidePx;
  const res = await fetch(`${API}/dither/preview`, { ...json("POST", body), signal });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const b = await res.json(); if (b.error) msg = b.error; } catch { /* png or empty */ }
    throw new Error(msg);
  }
  const faceBboxes = JSON.parse(res.headers.get("X-Face-Bboxes") || "[]");
  const blobUrl = URL.createObjectURL(await res.blob());
  return { blobUrl, faceBboxes };
}

// Upload ONE file as an inert draft for the editor. Resolves with {name}.
export function uploadDraft(file) {
  const fd = new FormData();
  fd.append("file", file);
  return req("/upload_draft", { method: "POST", body: fd });
}

// Commit the editor's per-image config + crop; the image (re)renders. image may be
// null ("use the classifier's decision"); editCrop is {rotation_quarters, rect, target}.
// mono (optional) is the per-image E1003 tone override (short knob names) or null to
// clear it; omit the arg entirely to leave any existing mono override untouched.
export const editImage = (name, image, editCrop, mono, editCropMono) => {
  const body = { image, edit_crop: editCrop };
  if (mono !== undefined) body.mono = mono;
  // per-image E1003 crop override: null clears it (mono follows the colour crop);
  // undefined omits the field entirely (leaves any existing override untouched).
  if (editCropMono !== undefined) body.edit_crop_mono = editCropMono;
  return req(`/image/${encodeURIComponent(name)}/edit`, json("POST", body));
};
