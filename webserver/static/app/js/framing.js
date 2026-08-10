// framing.js — the client's mirror of the server's framing decision, for labelling a
// photo×frame pairing in the UI. Nothing here renders a photo; it only describes what the
// server already did, so the words under a preview can never disagree with the picture.
//
// The server's rule (hokku_server/image_abc.frame_decision) is two lines: content is composed
// upright at the FRAME's orientation, and crop_to_fill_threshold alone decides cover-fill vs.
// letterbox. Both panels are exactly 4:3 (colour 1200×1600, mono 1872×1404), so the zoom
// arithmetic below is panel-independent — the cover÷fit ratio is invariant to a common scale,
// which is why plain 4:3 units give the same answer as real pixel dims.

// The server's rounding slack (_ZOOM_SNAP_RATIO): whole-pixel dimensions mean a photo that is
// the frame's aspect on paper can ask for a hair of zoom. Kept identical here so the label
// never claims bars the render doesn't have.
export const ZOOM_SNAP = 0.005;

// Did the editor crop apply on THIS frame? A crop is authored for one shape and is skipped on
// a mount of the other (the photo then renders as the original). Mono tries its own forked
// crop first, then falls back to the colour crop — mirroring flask_app._mono_crop_for.
function cropApplies(entry, framePortrait, mono) {
  const want = framePortrait ? "portrait" : "landscape";
  const candidates = mono ? [entry.crop_target_mono, entry.crop_target] : [entry.crop_target];
  for (const t of candidates) {
    if (t === undefined || t === null) continue;
    if (t === want) return true;
  }
  // A legacy crop stored without a target applies everywhere (the server treats a missing
  // target as unscoped), and `edited` is the only signal the payload carries for it.
  return !!entry.edited && !entry.crop_target && !entry.crop_target_mono;
}

/**
 * Describe how `entry` lands on a frame.
 * @returns null when the photo's dimensions are unknown, else
 *   {zoom, exact, fills, mismatch, w, h} — `zoom` is the extra scale needed to cover the
 *   frame (0.78 = 78%), `fills` whether the threshold allows it, `mismatch` whether the
 *   photo's own shape disagrees with the frame's.
 */
export function framingFor(entry, framePortrait, mono, threshold) {
  const w = entry && entry.image_width, h = entry && entry.image_height;
  if (!w || !h) return null;

  // A crop that applies was cut to the panel's own aspect (editor.js locks the box to 4/3 or
  // 3/4), so it covers with no zoom at all — the one case we can answer without measuring.
  if (cropApplies(entry, framePortrait, mono)) {
    return { zoom: 0, exact: true, fills: true, mismatch: false, w, h };
  }

  const vw = framePortrait ? 3 : 4, vh = framePortrait ? 4 : 3;
  const fit = Math.min(vw / w, vh / h), cover = Math.max(vw / w, vh / h);
  const zoom = cover / fit - 1;
  const thr = typeof threshold === "number" ? threshold : 0;
  // Square photos count as a mismatch on either frame — their shape is neither, and they do
  // lose a third of the frame to a fill, which is worth showing.
  const mismatch = w === h || (h > w) !== !!framePortrait;
  return { zoom, exact: zoom <= ZOOM_SNAP, fills: zoom <= thr + ZOOM_SNAP, mismatch, w, h };
}

// The photo's own shape at its TRUE proportions — a 16:9 reads visibly wider than a 3:2, so
// the size of the mismatch lands before the number does. Extreme panoramas are clamped so one
// photo can't stretch the row. One glyph, one meaning: it says SHAPE and nothing else.
export function shapeGlyph(w, h) {
  const H = 11, T = 1.3, P = 1.6;
  const gw = Math.max(5, Math.min(26, Math.round(H * w / h)));
  const bw = gw + P * 2, bh = H + P * 2, o = P + T / 2;
  return `<svg class="fpv-gly" width="${bw}" height="${bh}" viewBox="0 0 ${bw} ${bh}" aria-hidden="true">`
    + `<rect x="${o}" y="${o}" width="${gw - T}" height="${H - T}" rx="1.4" fill="none" `
    + `stroke="currentColor" stroke-width="${T}"/></svg>`;
}

// The label: what the eye can't see. Bars are visible, so they're never named — what's named
// is whether anything was lost (`Exact fit` vs `Zoomed 71%`) and, when the photo letterboxes,
// the slider value that would fill it (`Needs 113%`) — the same unit the setting uses.
export function framingLabel(f) {
  if (!f) return "";
  if (f.exact) return "Exact fit";
  const pct = Math.round(f.zoom * 100);
  return (f.fills ? "Zoomed " : "Needs ") + pct + "%";
}

/** The finished chip for the frame-preview modal, or "" when dimensions are unknown. */
export function framingChipHTML(entry, framePortrait, mono, threshold) {
  const f = framingFor(entry, framePortrait, mono, threshold);
  if (!f) return "";
  const glyph = f.mismatch ? shapeGlyph(f.w, f.h) : "";
  const title = f.exact
    ? "Fills the frame with nothing cropped away"
    : f.fills
      ? `Zoomed in ${Math.round(f.zoom * 100)}% past fit to remove the bars — the edges are cropped`
      : `Letterboxed: filling would need ${Math.round(f.zoom * 100)}% zoom, above the Zoom to fill limit`;
  return `<span class="fpv-chip fit" title="${title}">${glyph}${framingLabel(f)}</span>`;
}
