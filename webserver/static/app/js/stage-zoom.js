// stage-zoom.js — Fit ↔ 100% zoom for a live-preview stage: the custom dither editor and
// the E1003 tone editor (both desktop-only). Same controls as the photo editor's Color
// preview: one magnifier button, Z, and a double-click; drag pans while zoomed. No wheel or
// pinch. "100%" = one panel pixel per screen point (CSS px). The photo editor keeps its own
// copy in editor.js, which this module doesn't touch.
//
// stageZoom({ stage, wrap, img, panelPx, isOpen, isPeeking?, onZoom? })
//   stage     the preview box (position:relative; the controls are added to it)
//   wrap      the element that is transformed (holds img, plus any overlay that must track it)
//   img       the preview image; its content width (minus padding) is the Fit size
//   panelPx   () => how many panel pixels span the image's content width
//   isOpen    () => whether the editor is showing (gates the Z key)
//   isPeeking () => true while a press-and-hold compare is showing (no panning then)
//   onZoom    (zoomed) => called when Fit ↔ zoomed changes (e.g. fetch a full-size render)

const HOLD_MS = 280, MOVE_TOL = 12;     // a press is a tap only if released sooner / moved less
const DBL_MS = 300, DBL_TOL = 24;       // two taps this close in time and space = double-click
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function stageZoom({ stage, wrap, img, panelPx, isOpen, isPeeking = () => false, onZoom = () => {} }) {
  const pct = document.createElement("span");
  pct.className = "pv-pct"; pct.hidden = true;
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "pv-ctl pv-zoom"; btn.hidden = true;
  btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg><span>1:1</span>';
  stage.append(pct, btn);
  const btnT = btn.querySelector("span");

  let z = 1, x = 0, y = 0, ready = false, animT = null;

  // the zoom (× Fit) at which the image shows one panel pixel per screen point
  function z100() {
    const cs = getComputedStyle(img);
    const w = img.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const px = panelPx();
    return w > 0 && px > 0 ? Math.max(1, px / w) : 1;
  }
  function apply() {
    wrap.style.transform = z === 1 ? "" : `translate(${x}px,${y}px) scale(${z})`;
    stage.classList.toggle("pv-zoomed", z > 1);
    wrap.classList.toggle("pv-px", z > 1 && z >= z100() - 0.01);   // crisp panel dots from 100% up
    btn.hidden = !ready;
    btnT.textContent = z > 1 ? "Fit" : "1:1";
    btn.setAttribute("aria-label", z > 1 ? "Zoom to fit" : "Zoom to 100%");
    pct.hidden = !ready || z === 1;
    if (!pct.hidden) pct.textContent = Math.round((z / z100()) * 100) + "%";
  }
  function clampPan() {   // keep the scaled image from panning out of the stage
    const mx = Math.max(0, (wrap.offsetWidth * z - stage.clientWidth) / 2);
    const my = Math.max(0, (wrap.offsetHeight * z - stage.clientHeight) / 2);
    x = clamp(x, -mx, mx); y = clamp(y, -my, my);
  }
  function zoomTo(nz, cx, cy) {   // to an absolute zoom, keeping the screen point (cx, cy) still
    const was = z > 1;
    const s = stage.getBoundingClientRect();
    const ox = cx - s.left - s.width / 2, oy = cy - s.top - s.height / 2;
    x = ox - (ox - x) * (nz / z); y = oy - (oy - y) * (nz / z);
    z = nz;
    if (z === 1) { x = 0; y = 0; }
    clampPan(); apply();
    if (was !== z > 1) onZoom(z > 1);
  }
  function toggle(cx, cy) {
    if (!ready) return;
    wrap.classList.add("pv-anim");
    clearTimeout(animT); animT = setTimeout(() => wrap.classList.remove("pv-anim"), 260);
    zoomTo(z > 1 ? 1 : z100(), cx, cy);
  }
  const toggleCentre = () => { const r = stage.getBoundingClientRect(); toggle(r.left + r.width / 2, r.top + r.height / 2); };

  // the button is a control, not part of the photo: no press-and-hold, pan or double-click
  btn.addEventListener("pointerdown", (e) => e.stopPropagation());
  btn.addEventListener("click", (e) => { e.stopPropagation(); toggleCentre(); });

  // Z toggles, except while typing (a focused slider or switch still lets it through) and
  // never with Ctrl/⌘/Alt (Ctrl/⌘+Z is undo)
  document.addEventListener("keydown", (e) => {
    if (!ready || !isOpen()) return;
    if ((e.key !== "z" && e.key !== "Z") || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    const t = e.target;
    if (t && (t.isContentEditable || t.tagName === "TEXTAREA" || t.tagName === "SELECT" ||
        (t.tagName === "INPUT" && !/^(range|checkbox|radio|button)$/.test(t.type)))) return;
    e.preventDefault(); toggleCentre();
  });

  // Double-click and drag-to-pan from raw pointer events. Never the browser's dblclick:
  // it can fire after a HELD second press, i.e. right after a press-and-hold compare.
  let press = null, lastTap = null;
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    press = { t: e.timeStamp, x: e.clientX, y: e.clientY, moved: false, px: x, py: y };
    try { stage.setPointerCapture(e.pointerId); } catch (_) {}
  });
  stage.addEventListener("pointermove", (e) => {
    if (!press) return;
    const dx = e.clientX - press.x, dy = e.clientY - press.y;
    if (Math.hypot(dx, dy) > MOVE_TOL) press.moved = true;
    if (z > 1 && !isPeeking()) { x = press.px + dx; y = press.py + dy; clampPan(); apply(); }
  });
  function endPress(e) {
    const p = press; press = null;
    if (!p) return;
    const tap = e.type === "pointerup" && !p.moved && e.timeStamp - p.t < HOLD_MS;
    if (!tap) { lastTap = null; return; }
    if (lastTap && e.timeStamp - lastTap.t < DBL_MS && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < DBL_TOL) {
      lastTap = null; toggle(e.clientX, e.clientY);
    } else lastTap = { t: e.timeStamp, x: e.clientX, y: e.clientY };
  }
  stage.addEventListener("pointerup", endPress);
  stage.addEventListener("pointercancel", endPress);

  return {
    get zoomed() { return z > 1; },
    /** show the controls once there's a render to zoom (false hides them and returns to Fit) */
    setReady(v) { ready = !!v; if (!ready) this.reset(); else apply(); },
    /** back to Fit without animating (new photo, editor closed) */
    reset() { const was = z > 1; z = 1; x = 0; y = 0; lastTap = null; apply(); if (was) onZoom(false); },
  };
}
