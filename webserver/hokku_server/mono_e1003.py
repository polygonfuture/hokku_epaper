"""Minimal mono16 wire-format renderer for the reTerminal E1003 client (M2 hook).

Deliberately additive and self-contained — the general multi-panel
``PanelProfile`` refactor is a separate track. This module renders an
original library image straight to the E1003's wire format:

  * panel is NATIVE LANDSCAPE 1872 x 1404 (controller-reported; verified on
    hardware — marketing quotes the portrait "1404x1872", the IT8951 doesn't)
  * 16-level grayscale, Floyd-Steinberg dithered
  * 4 bits per pixel, 2 px/byte, HIGH nibble = left pixel of the pair
    (0x0 black ... 0xF white) — matches the firmware's it8951.c and was
    validated on glass by the M1 ramp (marker top-left, readable text)
  * exactly MONO_BYTES = 1,314,144 bytes, streamed top row first

Color inputs are converted to grayscale first (single-L-channel pipeline, so
the Spectra per-channel color-cast issue structurally cannot occur here).
Portrait sources are rotated 90 degrees into the landscape buffer (view the
panel rotated); flip PORTRAIT_ROTATE if the on-glass rotation direction is
the wrong way for how the E1003 is physically stood.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from numba import njit
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

MONO_PANEL_TYPE = "mono16_e1003"
MONO_W = 1872
MONO_H = 1404
MONO_BYTES = MONO_W * MONO_H // 2  # 1,314,144

# A source whose filename stem contains this marker is a step-wedge calibration
# target: it is packed to the wire format with NO tone map / dither / stretch /
# sharpen — every pixel snaps to its nearest of the 16 uniform panel levels, so
# the glass shows the TRUE hardware levels and a photo of it measures the
# panel's transfer curve (M4). Keeps the door open for periodic recalibration.
CALIB_MARKER = "e1003ramp"

# PIL rotation applied to portrait (taller-than-wide) sources so they fill the
# landscape buffer edge-to-edge. ROTATE_90 = counter-clockwise; change to
# Image.Transpose.ROTATE_270 if portraits come out upside-down on the glass.
PORTRAIT_ROTATE = Image.Transpose.ROTATE_90

# ── Taste knobs (applied ON TOP of the measured calibration) ─────────────
# The calibration makes tones faithful; these add deliberate punch, like the
# Spectra pipeline's contrast/sharpen steps. SenseCraft-style uncalibrated
# pipelines read "contrastier" because the panel's dark mids crush their
# midtones — these knobs recover that pop without giving up tonal accuracy.
# CONTRAST_GAIN: S-curve strength in L* (1.0 = off; 1.2 ≈ mild film curve).
# MIDTONE_TARGET_L / MAX_DARKEN_GAMMA: adaptive midtone placement — high-key
#   sources (e.g. a thin film scan whose backdrop sits at 98%) get gamma-
#   pulled so the image median lands near a print-like L*; never lifts.
#   This is the SenseCraft-look ingredient: their platform darkens mids hard.
# SHARPEN_*:     unsharp mask before dithering (percent 0 = off).
CONTRAST_GAIN = 1.15
MIDTONE_TARGET_L = 62.0
# On-glass tuning 2026-07-19 (a film-portrait scan; high-key source → the clamp,
# not the target, governs): 2.2 "a little too dark" → 1.85 "much closer,
# still subtly dark" → 1.70 "one more step" → 1.55 "close" → 1.40 approved.
# This clamp is the effective strength knob for bright scans; each 0.15 step
# ≈ 2 L* in mids. User-judged sweet spot 1.40–1.55, some photos up to ~1.8 —
# future editor preset should expose this as a slider 1.0 (off) … 1.8.
MAX_DARKEN_GAMMA = 1.40
# Sharpen tuning 2026-07-19 (a soft-focus film portrait): a big
# radius (2.0–2.5) is a LOCAL-CONTRAST/"clarity" pass — it wraps every edge in
# a halo, which on a soft base reads as "oversharpened but still blurry". The
# fix for a downscaled image is a SMALL-radius acutance pass: restore edge
# crispness at the pixel scale without halos. radius = WIDTH (halos live here —
# keep small); percent = STRENGTH (can run high at a small radius). Old 2.0/150
# haloed because of the radius, not the amount. On-glass ladder: 110 → 140 →
# 170 ("a little more" each time). 170 is crisp and still clean; 200 starts
# amplifying the FS dither grain in smooth/bokeh areas (the practical ceiling).
# Threshold keeps smooth skin/sky from amplifying film grain + the FS dither.
# NOTE: a soft source can't be un-blurred — honest acutance only.
# Future editor: expose radius + percent as a pre-dither sharpening slider.
SHARPEN_RADIUS = 1.2
SHARPEN_PERCENT = 170
SHARPEN_THRESHOLD = 2
# CLARITY: local-contrast pass = a LARGE-radius unsharp mask (the opposite end of
# the radius axis from acutance sharpening). On a short-throw panel this is the #1
# lever for a "wide dynamic range" feel — it lifts separation between regions
# without touching the endpoints or clipping. percent 0 = off.
CLARITY_RADIUS = 60


def _s_curve(t: np.ndarray, gain: float) -> np.ndarray:
    """Symmetric bias/gain S-curve on t in 0..1 (gain 1 = identity)."""
    if gain == 1.0:
        return t
    tg = np.power(t, gain)
    return tg / (tg + np.power(1.0 - t, gain))


def _tone_map_lstar(
    l_img: np.ndarray,
    *,
    darken_gamma: float = MAX_DARKEN_GAMMA,
    contrast: float = CONTRAST_GAIN,
    midtone: float = 1.0,
    black_point: float = 0.0,
    white_point: float = 0.0,
    highlights: float = 0.0,
    shadows: float = 0.0,
) -> np.ndarray:
    """Full tone treatment in L* (0..100) on a normalized tone t in [0,1]. Every
    user knob DEFAULTS TO A NO-OP so the panel's default look is unchanged until a
    slider moves. Stage order — adaptive darken (preset baseline) → endpoint levels
    → midtone gamma → shadow/highlight roll → contrast S-curve:

      - ``darken_gamma`` — adaptive midtone darkening toward MIDTONE_TARGET_L
        (darken-only, clamped; the Faithful/Punchy preset baseline).
      - ``black_point`` / ``white_point`` — bidirectional endpoint levels. +black
        LIFTS the black floor (opens/greys shadows); −black raises the input black
        (deepens/crushes). +white pushes highlights toward paper white; −white
        pulls the white level down. Both 0 = off.
      - ``midtone`` — direct gamma on t (>1 darkens mids, <1 brightens; 1.0 = off).
      - ``shadows`` / ``highlights`` — regional roll (+=brighten / −=darken).
        +shadows lifts the shadow region; −highlights recovers (pulls down) blown
        highlights, +highlights brightens them.
      - ``contrast`` — S-curve strength (1.0 = linear; CONTRAST_GAIN = default).
    """
    t = np.clip(l_img / 100.0, 1e-6, 1.0)
    # 1. adaptive midtone darkening (preset baseline; preserves endpoints)
    med = float(np.median(t))
    if 0.0 < med < 1.0 and med > MIDTONE_TARGET_L / 100.0:
        g = np.log(MIDTONE_TARGET_L / 100.0) / np.log(med)
        g = min(max(g, 1.0), darken_gamma)
        t = np.power(t, g)
    # 2. endpoint levels — one input + one output remap from the two ± knobs
    if black_point or white_point:
        in_lo, in_hi = max(0.0, -black_point), 1.0 - max(0.0, white_point)
        if in_hi - in_lo > 1e-3:
            t = np.clip((t - in_lo) / (in_hi - in_lo), 0.0, 1.0)
        out_lo, out_hi = max(0.0, black_point), 1.0 - max(0.0, -white_point)
        t = out_lo + t * (out_hi - out_lo)
    # 3. direct midtone gamma
    if midtone != 1.0:
        t = np.power(np.clip(t, 1e-6, 1.0), midtone)
    # 4. regional shadow / highlight roll — both follow the +=brighten / −=darken
    # convention (Lightroom-style): +shadows lifts the shadow region, +highlights
    # brightens (−highlights recovers/pulls-down) the highlight region.
    if shadows:
        t = np.clip(t + shadows * (1.0 - t) ** 2, 0.0, 1.0)
    if highlights:
        t = np.clip(t + highlights * t ** 2, 0.0, 1.0)
    # 5. global contrast S-curve (1.0 = skip)
    if contrast and contrast != 1.0:
        t = _s_curve(t, contrast)
    return 100.0 * t

# 16 evenly spaced gray levels 0..255 (0, 17, ... 255): nibble value == index.
_LEVELS = [round(i * 255 / 15) for i in range(16)]

# ── Measured panel tone calibration (ED103TC2) ──────────────────────────
# The panel's 16 levels are NOT uniform sRGB steps. Calibration measured by
# aitjcize/epaper-image-convert on this exact panel: full black shows
# Y=0.009, full white Y=0.65 (relative luminance), and the level ramp runs
# in CIE L* with a 1.42 exponent (panel mids darker than L*-linear):
#     L*(i) = L*black + (i/15)^1.42 · (L*white − L*black)
# We dither the image's linear luminance against these PERCEIVED level
# luminances, after compressing the image's range into the panel envelope
# in L* — the same measured-palette + panel-anchored-DRC architecture the
# Hokku Spectra pipeline uses. Dithering happens in LINEAR LIGHT (physical
# ink mixes average in reflectance, not in sRGB code).
_PANEL_BLACK_Y = 0.009
_PANEL_WHITE_Y = 0.65
_PANEL_GAMMA = 1.42


def _lstar_from_y(y: np.ndarray | float):
    y = np.asarray(y, dtype=np.float64)
    return np.where(y > 0.008856, 116.0 * np.cbrt(y) - 16.0, 903.3 * y)


def _y_from_lstar(L: np.ndarray | float):
    L = np.asarray(L, dtype=np.float64)
    return np.where(L > 8.0, ((L + 16.0) / 116.0) ** 3, L / 903.3)


def _srgb_to_linear(v: np.ndarray):
    s = np.asarray(v, dtype=np.float64) / 255.0
    return np.where(s > 0.04045, ((s + 0.055) / 1.055) ** 2.4, s / 12.92)


_LB = float(_lstar_from_y(_PANEL_BLACK_Y))   # ≈ 8.1
_LW = float(_lstar_from_y(_PANEL_WHITE_Y))   # ≈ 84.5

# Two dither palettes — the level luminances the FS dither targets:
#  • UNIFORM: levels assumed evenly spaced in sRGB code. The eye-tuned default;
#    the panel's real darkness is absorbed into MAX_DARKEN_GAMMA on glass.
#  • MEASURED: THIS unit's real per-level reflectance, measured 2026-07-19 via
#    the step wedge + ProRAW (scratchpad/calib_localwhite.py) — relative to
#    paper white, level0 ≈ 0.143 (panel black ≈14% of white → crushed shadows)
#    rising per the fitted curve. Dithering against the true reflectances makes
#    tone reproduction faithful instead of assuming uniform steps. The earlier
#    aitjcize model (BLACK_Y 0.009 / WHITE_Y 0.65 / γ1.42) was rejected on glass
#    (washed out) and is superseded by these own-unit numbers.
# Selected PER-REQUEST (render_mono_bin arg, driven by config) so the two can be
# A/B'd on glass without recompiling.
_LEVELS_LINEAR_UNIFORM = _srgb_to_linear(np.asarray(_LEVELS)).astype(np.float32)
_MEASURED_NORM = np.array([0.000, 0.039, 0.081, 0.090, 0.134, 0.201, 0.254, 0.295,
                           0.372, 0.469, 0.541, 0.618, 0.687, 0.860, 0.882, 1.000])
_MEASURED_BLACK_REFL = 0.143   # level0 reflectance ÷ paper-white reflectance
_LEVELS_LINEAR_MEASURED = (_MEASURED_BLACK_REFL
                           + _MEASURED_NORM * (1.0 - _MEASURED_BLACK_REFL)).astype(np.float32)

# Module default (uniform). Kept as attrs for the decode ramp + tests; the live
# choice is the render_mono_bin `use_measured_ramp` arg.
USE_MEASURED_RAMP = False
_LEVELS_LINEAR = _LEVELS_LINEAR_UNIFORM


@njit(cache=True)
def _fs_dither_linear(img, pal):  # pragma: no cover — numba-compiled
    """Serpentine Floyd-Steinberg over a linear-light float image against a
    linear-light palette. Returns uint8 level indices."""
    h, w = img.shape
    out = np.empty((h, w), dtype=np.uint8)
    buf = img.copy()
    n = pal.shape[0]
    for y in range(h):
        l2r = (y % 2) == 0
        for i in range(w):
            x = i if l2r else (w - 1 - i)
            v = buf[y, x]
            # nearest palette entry (pal is ascending)
            best = 0
            bd = abs(v - pal[0])
            for k in range(1, n):
                d = abs(v - pal[k])
                if d < bd:
                    bd = d
                    best = k
            out[y, x] = best
            err = v - pal[best]
            step = 1 if l2r else -1
            if 0 <= x + step < w:
                buf[y, x + step] += err * (7.0 / 16.0)
            if y + 1 < h:
                if 0 <= x - step < w:
                    buf[y + 1, x - step] += err * (3.0 / 16.0)
                buf[y + 1, x] += err * (5.0 / 16.0)
                if 0 <= x + step < w:
                    buf[y + 1, x + step] += err * (1.0 / 16.0)
    return out

# Tiny render cache: key -> wire bytes. A frame polls the same image repeatedly
# between schedule ticks; re-dithering 2.6 MP each call would be pure waste.
# Bounded FIFO — each entry is ~1.3 MB. The sharpen params are part of the key:
# a per-image / preset sharpen change must not serve a stale render.
_CACHE_MAX = 6
_cache: dict[tuple, bytes] = {}
_cache_lock = threading.Lock()


def invalidate(path: Path) -> int:
    """Drop every cached mono render for one source file. Call when a photo's edit changes
    (commit_edit / set_edit_crop_mono / set_edit_mono) so the mono frame never serves a stale
    render — the render cache key is keyed on the source path + edit params, but an edit that
    changes only metadata must actively evict here (the source mtime/size don't change).

    Returns the number of entries dropped. Also clears the mono_tone staged preview cache for
    the same path."""
    key_path = str(path)
    with _cache_lock:
        stale = [k for k in _cache if k and k[0] == key_path]
        for k in stale:
            _cache.pop(k, None)
    try:
        from . import mono_tone
        mono_tone.invalidate(path)
    except Exception:
        pass
    return len(stale)


def _calibration_bin(path: Path) -> bytes:
    """Pack an exact-level step-wedge target — NO tone map / dither / stretch /
    sharpen. Every pixel snaps to its nearest of the 16 uniform panel levels, so
    on glass the patches ARE the true hardware levels (see CALIB_MARKER). The
    target must be authored at MONO_W x MONO_H so patch edges stay crisp; any
    other size is nearest-neighbour resized (no interpolation across levels)."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, "calib")
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        gray = img.convert("L")
    if gray.size != (MONO_W, MONO_H):
        gray = gray.resize((MONO_W, MONO_H), Image.Resampling.NEAREST)
    arr = np.asarray(gray, dtype=np.float32)
    idx = np.clip(np.rint(arr / 255.0 * 15.0), 0, 15).astype(np.uint8)
    packed = ((idx[:, 0::2] << 4) | (idx[:, 1::2] & 0x0F)).astype(np.uint8)
    data = packed.tobytes()
    assert len(data) == MONO_BYTES
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = data
    return data


def _dtcore_key(d):
    """Hashable, rounded rep of the dtcore control dict for the render cache."""
    if d is None:
        return None
    def norm(x):
        if isinstance(x, dict):
            return tuple(sorted((k, norm(v)) for k, v in x.items()))
        if isinstance(x, bool):
            return x
        if isinstance(x, float):
            return round(x, 4)
        return x
    return norm(d)


def _norm_crop_rect(crop_rect):
    """Validate a normalized (x, y, w, h) crop rect; None (or a degenerate rect) means
    'no crop' (auto fit/letterbox). Coordinates are in the ROTATED frame, 0..1."""
    if not crop_rect or len(crop_rect) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in crop_rect)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # a full-frame rect is a no-op crop — treat as None so it inherits the auto path
    if x <= 0 and y <= 0 and w >= 1 and h >= 1:
        return None
    return (x, y, w, h)


def _crop_key(crop_rect):
    """Hashable rounded rep of a crop rect for the render cache."""
    if crop_rect is None:
        return None
    return tuple(round(v, 4) for v in crop_rect)


def _anchor_key(bboxes_norm):
    """Hashable rep of the face-aware crop anchor for the render cache — the same photo
    aimed at a face vs at the centre is a different render."""
    if not bboxes_norm:
        return None
    return tuple(
        (round(b.x, 4), round(b.y, 4), round(b.w, 4), round(b.h, 4)) for b in bboxes_norm
    )


def _mono_content_portrait(src_w, src_h, rotation_quarters, crop_rect,
                           frame_portrait=False):
    """Whether the render composed PORTRAIT content (rotated 90° into the landscape wire
    buffer). Mirrors _fit_gray_to_panel's decision EXACTLY so previews un-rotate correctly.

    Takes the RAW source (w, h) + the manual edit and derives the post-crop orientation via
    the shared ``effective_cropped_dims`` — the SAME shape _fit_gray_to_panel measures after
    applying the crop — then runs the shared ``frame_decision``. This single-source-of-truth
    for the cropped shape is what keeps the preview un-rotate in lock-step with the render
    (a raw-vs-cropped mismatch here was the sideways-preview bug on edited photos)."""
    from .image_abc import effective_cropped_dims, frame_decision

    ew, eh = effective_cropped_dims(src_w, src_h, rotation_quarters, crop_rect)
    return frame_decision(
        ew, eh, MONO_W, MONO_H,
        frame_portrait=bool(frame_portrait),
        crop_to_fill_threshold=0.0,
    ).content_portrait


def _mono_fit_centering(gray, cw, ch, crop_anchor_bboxes_norm):
    """``ImageOps.fit`` centering (fx, fy) that reproduces the colour path's face-aware
    cover offset. Returns the blind centre when there are no faces or no slack to move.

    ``fit`` crops the overflow at ``(centering * overflow)``, so converting the shared
    ``_face_centered_crop_offset`` result to a fraction of the overflow keeps the two
    pipelines aiming at the same place.
    """
    if not crop_anchor_bboxes_norm:
        return (0.5, 0.5)
    from .image_abc import _face_centered_crop_offset

    scale = max(cw / gray.width, ch / gray.height)
    scaled_w, scaled_h = gray.width * scale, gray.height * scale
    x_off, y_off = _face_centered_crop_offset(
        crop_anchor_bboxes_norm, scaled_w, scaled_h, cw, ch
    )
    over_w, over_h = scaled_w - cw, scaled_h - ch
    fx = x_off / over_w if over_w > 0 else 0.5
    fy = y_off / over_h if over_h > 0 else 0.5
    return (min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0))


def _fit_gray_to_panel(gray, tw, th, rotation_quarters, crop_rect,
                       frame_portrait=False,
                       crop_to_fill_threshold=0.0,
                       crop_anchor_bboxes_norm=None):
    """Bring a grayscale source to the (tw, th) LANDSCAPE wire buffer (tw>=th).

    The E1003 panel is physically landscape and the firmware does no rotation, so a
    PORTRAIT-oriented result must be composed upright on a portrait content canvas and
    then rotated 90° into the landscape wire buffer (view by turning the frame). This
    mirrors the colour path's "portrait working-canvas → final rotate".

    Framing is the SHARED ``frame_decision`` (identical policy to the colour pipeline):
    any manual crop/rotation is applied FIRST, then content is composed upright for the
    FRAME's orientation; a real crop forces cover, otherwise the single
    ``crop_to_fill_threshold`` decides cover-vs-letterbox.
    Only ``ImageOps.fit``/``contain`` + the ``PORTRAIT_ROTATE`` are mono-local coordinate
    work; the decision is shared."""
    from .image_abc import _apply_crop_rotation, frame_decision

    if crop_rect is not None or rotation_quarters:
        gray = _apply_crop_rotation(gray, rotation_quarters, crop_rect)
        # the anchor is in ORIGINAL coords — ride it through the same crop as the pixels
        if crop_anchor_bboxes_norm:
            from .image_abc import _transform_keepout_through_crop

            crop_anchor_bboxes_norm = (
                _transform_keepout_through_crop(
                    crop_anchor_bboxes_norm, rotation_quarters, crop_rect
                )
                or None
            )

    decision = frame_decision(
        gray.width, gray.height, tw, th,
        frame_portrait=bool(frame_portrait),
        crop_to_fill_threshold=crop_to_fill_threshold,
    )
    # content canvas: landscape = (tw, th); portrait = (th, tw) (rotated into the buffer)
    cw, ch = (th, tw) if decision.content_portrait else (tw, th)

    if decision.use_cover:
        # ImageOps.fit's `centering` is the mono equivalent of the colour path's crop
        # offset: (0.5, 0.5) is the blind centre; face-aware aims it at the face union so
        # a fill loses background rather than someone's head.
        content = ImageOps.fit(
            gray, (cw, ch), Image.Resampling.LANCZOS,
            centering=_mono_fit_centering(gray, cw, ch, crop_anchor_bboxes_norm),
        )
    else:
        content = ImageOps.contain(gray, (cw, ch), Image.Resampling.LANCZOS)

    # portrait content is composed upright on a (th, tw) canvas — rotate 90° into the
    # (tw, th) landscape wire buffer so the firmware displays it correctly when the
    # frame is physically turned to portrait.
    if decision.content_portrait:
        content = content.transpose(PORTRAIT_ROTATE)
    return content


def render_mono_bin(
    path: Path,
    *,
    sharpen_radius: float = SHARPEN_RADIUS,
    sharpen_percent: float = SHARPEN_PERCENT,
    sharpen_threshold: int = SHARPEN_THRESHOLD,
    use_measured_ramp: bool = USE_MEASURED_RAMP,
    darken_gamma: float = MAX_DARKEN_GAMMA,
    shadow_lift: float = 0.0,
    black_point: float = 0.0,
    white_point: float = 0.0,
    clarity: float = 0.0,
    contrast: float = CONTRAST_GAIN,
    midtone: float = 1.0,
    highlights: float = 0.0,
    shadows: float = 0.0,
    dtcore: dict | None = None,
    rotation_quarters: int = 0,
    crop_rect: tuple[float, float, float, float] | None = None,
    frame_portrait: bool = False,
    crop_to_fill_threshold: float = 0.0,
    crop_anchor_bboxes_norm=None,
) -> bytes:
    """Render an original image file to E1003 wire bytes (cached).

    When ``dtcore`` is given (the B&W Contrast / Custom profile), the tone stage
    is the verified darktable+Lightroom port in :mod:`mono_tone` instead of the
    legacy ramp path (which stays frozen as the "Faithful" preset). ``dtcore`` is
    ``{"sigmoid": {...}, "local_contrast": {...}, "basic": {...}}`` (render_tone
    kwargs); sharpening (Details) is the existing ``sharpen_*`` args, applied to
    the toned content before the mat.

    Sharpening defaults to the module constants (the on-glass-tuned 1.2/170/2).
    ``use_measured_ramp`` picks the dither palette (uniform vs this unit's
    measured level reflectances); ``darken_gamma`` is the midtone-darkening
    clamp. Both are passed in from config so the tunes can be A/B'd on glass.
    """
    if CALIB_MARKER in path.stem.lower():
        return _calibration_bin(path)

    # Clamp to sane bounds — the values arrive from config/UI, not just code.
    sharpen_radius = float(min(max(sharpen_radius, 0.0), 5.0))
    sharpen_percent = int(min(max(sharpen_percent, 0), 400))
    sharpen_threshold = int(min(max(sharpen_threshold, 0), 255))
    darken_gamma = float(min(max(darken_gamma, 1.0), 3.0))
    shadow_lift = float(min(max(shadow_lift, 0.0), 0.3))
    use_measured_ramp = bool(use_measured_ramp)
    black_point = float(min(max(black_point, -0.3), 0.3))
    white_point = float(min(max(white_point, -0.3), 0.3))
    clarity = float(min(max(clarity, 0.0), 100.0))
    contrast = float(min(max(contrast, 1.0), 1.8))
    midtone = float(min(max(midtone, 0.5), 2.0))
    highlights = float(min(max(highlights, -0.5), 0.5))
    shadows = float(min(max(shadows, -0.5), 0.5))
    rotation_quarters = int(rotation_quarters) % 4
    crop_rect = _norm_crop_rect(crop_rect)
    frame_portrait = bool(frame_portrait)
    from .image_abc import MAX_CROP_TO_FILL_THRESHOLD

    crop_to_fill_threshold = float(
        min(max(crop_to_fill_threshold, 0.0), MAX_CROP_TO_FILL_THRESHOLD)
    )

    st = path.stat()
    key = (
        str(path), st.st_mtime_ns, st.st_size,
        round(sharpen_radius, 3), sharpen_percent, sharpen_threshold,
        use_measured_ramp, round(darken_gamma, 3), round(shadow_lift, 3),
        round(black_point, 3), round(white_point, 3), round(clarity, 2),
        round(contrast, 3), round(midtone, 3), round(highlights, 3), round(shadows, 3),
        _dtcore_key(dtcore),
        rotation_quarters, _crop_key(crop_rect), frame_portrait,
        round(crop_to_fill_threshold, 3),
        _anchor_key(crop_anchor_bboxes_norm),
    )
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        # Grayscale FIRST — color sources become luminance before any
        # tone/dither step (explicit product requirement).
        gray = img.convert("L")

    # Editor crop (if any) frames the image to the panel; else auto fit/letterbox
    # governed by the single crop_to_fill_threshold (shared with colour via frame_decision).
    gray = _fit_gray_to_panel(gray, MONO_W, MONO_H, rotation_quarters, crop_rect,
                              frame_portrait, crop_to_fill_threshold,
                              crop_anchor_bboxes_norm)

    # ── dtcore tone path (B&W Contrast / Custom): darktable sigmoid + local-laplacian
    #    + Lightroom-matched Basic + CLAHE, run on the CONTENT (before the mat so the
    #    local-contrast never haloes the letterbox edge). Faithful uses the legacy path below. ──
    if dtcore is not None:
        from . import mono_tone
        toned = mono_tone.render_tone(
            np.asarray(gray),
            sigmoid=dtcore.get("sigmoid"),
            local_contrast=dtcore.get("local_contrast"),
            basic=dtcore.get("basic"),
        )
        out = Image.fromarray(toned, mode="L")
        if sharpen_percent > 0:  # Details: acutance sharpening on the toned content
            out = out.filter(ImageFilter.UnsharpMask(
                radius=sharpen_radius, percent=sharpen_percent, threshold=sharpen_threshold))
        if out.size != (MONO_W, MONO_H):
            mat = Image.new("L", (MONO_W, MONO_H), 255)
            mat.paste(out, ((MONO_W - out.width) // 2, (MONO_H - out.height) // 2))
            out = mat
        lin = _srgb_to_linear(np.asarray(out)).astype(np.float32)
        idx = _fs_dither_linear(lin, _LEVELS_LINEAR_UNIFORM)
        packed = ((idx[:, 0::2] << 4) | (idx[:, 1::2] & 0x0F)).astype(np.uint8)
        data = packed.tobytes()
        assert len(data) == MONO_BYTES, f"mono pack produced {len(data)} bytes"
        with _cache_lock:
            while len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[key] = data
        logger.info("mono16_e1003 (dtcore) rendered: %s (%d bytes)", path.name, len(data))
        return data

    # Black-point-only stretch. A full autocontrast pushed the image's natural
    # highlights (e.g. a light backdrop at ~83% tone) up to paper white, which
    # read as washed out next to SenseCraft's render — measured on-glass:
    # their backdrop sits at 0.83 of the white anchor, full-autocontrast ours
    # at 0.99. Anchoring only the shadows keeps highlight tone intact.
    arr = np.asarray(gray, dtype=np.float64)
    p1 = float(np.percentile(arr, 1))
    if p1 < 64:  # skip degenerate stretch on very bright/low-contrast images
        arr = np.clip((arr - p1) * (255.0 / (255.0 - p1)), 0, 255)
        gray = Image.fromarray(arr.astype(np.uint8), mode="L")
    # Clarity: large-radius local-contrast pass BEFORE acutance sharpening (the
    # two live at opposite ends of the radius axis). Applied to the content image
    # before any mat paste so the letterbox edge never haloes.
    if clarity > 0:
        gray = gray.filter(ImageFilter.UnsharpMask(
            radius=CLARITY_RADIUS, percent=int(round(clarity)), threshold=0))
    if sharpen_percent > 0:
        gray = gray.filter(ImageFilter.UnsharpMask(
            radius=sharpen_radius, percent=sharpen_percent, threshold=sharpen_threshold))
    if gray.size != (MONO_W, MONO_H):
        mat = Image.new("L", (MONO_W, MONO_H), 255)
        mat.paste(gray, ((MONO_W - gray.width) // 2, (MONO_H - gray.height) // 2))
        gray = mat

    # Tone map in L* (darken-only midtone + contrast S-curve), then
    # Floyd-Steinberg in linear light against the chosen level luminances.
    # UNIFORM palette lets the panel's real darkness ride on darken_gamma;
    # MEASURED palette targets the true per-level reflectances (faithful tone).
    levels = _LEVELS_LINEAR_MEASURED if use_measured_ramp else _LEVELS_LINEAR_UNIFORM
    y_img = _srgb_to_linear(np.asarray(gray))
    l_img = _tone_map_lstar(
        _lstar_from_y(y_img), darken_gamma=darken_gamma, contrast=contrast,
        midtone=midtone, black_point=black_point, white_point=white_point,
        highlights=highlights, shadows=shadows)
    lin = _y_from_lstar(l_img).astype(np.float32)
    if use_measured_ramp:
        # Envelope DRC: remap the image's [0,1] range into the panel's ACHIEVABLE
        # reflectance band [black_target, 1.0] before dithering. Without it every
        # target below the panel's 0.143 black floor clamps to level 0 and the
        # shadows crush to a detail-less blob. White stays true paper-white, so
        # (unlike the old aitjcize envelope that capped white at ~0.65) highlights
        # aren't washed out. shadow_lift raises black_target above the physical
        # floor so the darkest tones land on level 1-2 (dithered detail) instead
        # of a flat level-0 black — trades a touch of black depth for detail.
        black_target = _MEASURED_BLACK_REFL + shadow_lift
        lin = black_target + lin * (1.0 - black_target)
    idx = _fs_dither_linear(lin, levels)

    packed = ((idx[:, 0::2] << 4) | (idx[:, 1::2] & 0x0F)).astype(np.uint8)
    data = packed.tobytes()
    assert len(data) == MONO_BYTES, f"mono pack produced {len(data)} bytes"

    with _cache_lock:
        while len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = data
    logger.info("mono16_e1003 rendered: %s (%d bytes)", path.name, len(data))
    return data


def render_split_compare(
    path: Path,
    *,
    gamma_a: float = MAX_DARKEN_GAMMA,
    gamma_b: float = MAX_DARKEN_GAMMA,
    shadow_lift: float = 0.0,
    sharpen_radius: float = SHARPEN_RADIUS,
    sharpen_percent: float = SHARPEN_PERCENT,
    sharpen_threshold: int = SHARPEN_THRESHOLD,
) -> bytes:
    """Split-screen A/B of one image, divided down the middle:
      LEFT half  = Version A — uniform ramp (the eye-tuned look), gamma_a.
      RIGHT half = Version B — measured ramp + envelope, gamma_b + shadow_lift.
    A ~4 px black seam marks the divide. Both halves share the same crop/sharpen
    (same source pixels), so the split lands on identical features — you compare
    the two tone treatments on the exact same part of the image, on glass."""
    common = dict(sharpen_radius=sharpen_radius, sharpen_percent=sharpen_percent,
                  sharpen_threshold=sharpen_threshold)
    a = render_mono_bin(path, use_measured_ramp=False, darken_gamma=gamma_a, **common)
    b = render_mono_bin(path, use_measured_ramp=True, darken_gamma=gamma_b,
                        shadow_lift=shadow_lift, **common)
    A = np.frombuffer(a, dtype=np.uint8).reshape(MONO_H, MONO_W // 2).copy()
    B = np.frombuffer(b, dtype=np.uint8).reshape(MONO_H, MONO_W // 2)
    mid = (MONO_W // 2) // 2            # byte column of the x=936 centre line
    A[:, mid:] = B[:, mid:]             # right half from B
    A[:, mid - 1:mid + 1] = 0x00        # ~4 px black seam down the centre
    out = A.tobytes()
    assert len(out) == MONO_BYTES
    return out


def _linear_to_srgb(y: np.ndarray):
    y = np.asarray(y, dtype=np.float64)
    s = np.where(y <= 0.0031308, 12.92 * y, 1.055 * (y ** (1 / 2.4)) - 0.055)
    return np.clip(np.round(s * 255.0), 0, 255).astype(np.uint8)


# sRGB value each level looks like on-screen for the preview. We decode through
# the UNIFORM ramp for BOTH render palettes — this is validated against glass:
# the user A/B'd the two renders on the physical panel and the uniform-decoded
# preview matches what the panel shows. NOTE: decoding the measured render through
# the measured reflectances was tried and REJECTED — it showed measured/"punchy"
# as soft gray, contradicting the glass (where the measured ramp reads dark/crunchy).
# The measured calibration (0.143 black, glare-inflated) does not describe this
# unit's actual on-glass appearance, so it is NOT used for the decode.
_LEVELS_SRGB_PERCEIVED = _linear_to_srgb(_LEVELS_LINEAR_UNIFORM)


def mono_bin_to_image(data: bytes) -> Image.Image:
    """Unpack wire bytes to a glass-accurate grayscale image, exactly as the panel shows
    it (landscape 1872×1404). Portrait content reads SIDEWAYS here — use
    ``mono_bin_to_upright_image`` for a legible UI preview.

    Levels decode through the uniform perceived ramp — validated by the user
    against the physical panel for both render palettes."""
    if len(data) != MONO_BYTES:
        raise ValueError(f"expected {MONO_BYTES} bytes, got {len(data)}")
    packed = np.frombuffer(data, dtype=np.uint8).reshape(MONO_H, MONO_W // 2)
    idx = np.empty((MONO_H, MONO_W), dtype=np.uint8)
    idx[:, 0::2] = packed >> 4
    idx[:, 1::2] = packed & 0x0F
    return Image.fromarray(_LEVELS_SRGB_PERCEIVED[idx], mode="L")


def mono_bin_to_upright_image(data: bytes, content_portrait: bool) -> Image.Image:
    """Decode wire bytes to an UPRIGHT, legible preview for the UI. When the render
    composed portrait content (rotated 90° into the landscape buffer for a physically
    turned frame), un-rotate it back so the thumbnail/preview reads upright as a portrait
    image — instead of the sideways wire decode. Landscape content is returned as-is."""
    img = mono_bin_to_image(data)
    if content_portrait:
        img = img.transpose(Image.Transpose.ROTATE_270)   # inverse of PORTRAIT_ROTATE
    return img


def render_mono_preview_image(
    path: Path,
    *,
    dtcore: dict,
    sharpen_radius: float = SHARPEN_RADIUS,
    sharpen_percent: float = SHARPEN_PERCENT,
    sharpen_threshold: int = SHARPEN_THRESHOLD,
    max_side: int = 900,
    rotation_quarters: int = 0,
    crop_rect: tuple[float, float, float, float] | None = None,
    frame_portrait: bool = False,
    crop_to_fill_threshold: float = 0.0,
    crop_anchor_bboxes_norm=None,
    upright: bool = False,
) -> Image.Image:
    """Fast PREVIEW-only render of the dtcore (B&W Contrast / Custom) tone path.

    Runs the SAME pipeline as ``render_mono_bin``'s dtcore branch — fit/letterbox,
    ``mono_tone.render_tone`` (sigmoid + local-laplacian + Basic + CLAHE), acutance
    sharpen, dither, decode through the perceived ramp — but at ``max_side`` instead
    of the native 1872x1404, so the expensive local-laplacian runs on ~4x fewer
    pixels. Returns the glass-accurate L preview directly (no wire packing).

    PREVIEW-ONLY: the frame-serve / save path never calls this — it uses
    ``render_mono_bin`` at full res, byte-for-byte unchanged. A preview is allowed
    to differ marginally from the save (local contrast / CLAHE scale with resolution),
    exactly as it already does after the post-render LANCZOS downscale.
    """
    from . import mono_tone

    rotation_quarters = int(rotation_quarters) % 4
    crop_rect = _norm_crop_rect(crop_rect)
    frame_portrait = bool(frame_portrait)
    from .image_abc import MAX_CROP_TO_FILL_THRESHOLD

    crop_to_fill_threshold = float(
        min(max(crop_to_fill_threshold, 0.0), MAX_CROP_TO_FILL_THRESHOLD)
    )

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        gray = img.convert("L")

    # Preview canvas: the panel geometry scaled down so max(side) == max_side. The
    # tone pipeline + letterbox mat all run at this reduced size. Editor crop (if any)
    # frames the image to the panel; else the frame orientation + the single
    # crop_to_fill_threshold do (shared with colour via frame_decision).
    scale = min(1.0, max_side / float(MONO_W))
    pw, ph = max(1, round(MONO_W * scale)), max(1, round(MONO_H * scale))
    content = _fit_gray_to_panel(gray, pw, ph, rotation_quarters, crop_rect,
                                 frame_portrait, crop_to_fill_threshold,
                                 crop_anchor_bboxes_norm)

    # Staged tone: the fit content is identified by (path, mtime, size, crop) so a
    # Tone-page drag (basic_*/CLAHE only) reuses the cached sigmoid+local-laplacian result
    # and re-runs just the cheap Basic/CLAHE tail. Crop is part of the identity so a
    # different crop can't reuse a stale intermediate.
    st = path.stat()
    id_key = (str(path), st.st_mtime_ns, pw, ph, content.size, rotation_quarters, _crop_key(crop_rect), frame_portrait, round(crop_to_fill_threshold, 3), _anchor_key(crop_anchor_bboxes_norm))
    toned = mono_tone.render_tone_staged(
        np.asarray(content),
        id_key=id_key,
        sigmoid=dtcore.get("sigmoid"),
        local_contrast=dtcore.get("local_contrast"),
        basic=dtcore.get("basic"),
    )
    out = Image.fromarray(toned, mode="L")
    sharpen_percent = int(min(max(sharpen_percent, 0), 400))
    if sharpen_percent > 0:
        out = out.filter(ImageFilter.UnsharpMask(
            radius=float(min(max(sharpen_radius, 0.0), 5.0)),
            percent=sharpen_percent, threshold=int(min(max(sharpen_threshold, 0), 255))))
    if out.size != (pw, ph):
        mat = Image.new("L", (pw, ph), 255)
        mat.paste(out, ((pw - out.width) // 2, (ph - out.height) // 2))
        out = mat
    lin = _srgb_to_linear(np.asarray(out)).astype(np.float32)
    idx = _fs_dither_linear(lin, _LEVELS_LINEAR_UNIFORM)
    preview = Image.fromarray(_LEVELS_SRGB_PERCEIVED[idx], mode="L")
    # For a legible UI preview, un-rotate portrait content (composed sideways into the
    # landscape buffer) back to upright — so the editor shows the photo the right way up.
    if upright:
        if _mono_content_portrait(gray.width, gray.height, rotation_quarters, crop_rect, frame_portrait):
            preview = preview.transpose(Image.Transpose.ROTATE_270)
    return preview
