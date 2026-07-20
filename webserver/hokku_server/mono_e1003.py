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

# Above this fraction of lost image (from aspect mismatch), letterbox on a
# paper-white mat instead of cover-cropping. 0.12 keeps 3:2 photos full-bleed
# (~11% crop) while square/4:5 frames get the mat.
MAX_COVER_CROP_FRAC = 0.12

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


def _s_curve(t: np.ndarray, gain: float) -> np.ndarray:
    """Symmetric bias/gain S-curve on t in 0..1 (gain 1 = identity)."""
    if gain == 1.0:
        return t
    tg = np.power(t, gain)
    return tg / (tg + np.power(1.0 - t, gain))


def _tone_map_lstar(l_img: np.ndarray, darken_gamma: float = MAX_DARKEN_GAMMA) -> np.ndarray:
    """Full tone treatment in L* (0..100): adaptive midtone darkening toward
    MIDTONE_TARGET_L (darken-only, clamped by ``darken_gamma`` — preserves black
    and white endpoints), then the contrast S-curve. Shared by renderer + tests.
    ``darken_gamma`` is a per-request knob (config) so the darkening strength can
    be re-tuned when the measured palette is in play."""
    t = np.clip(l_img / 100.0, 1e-6, 1.0)
    med = float(np.median(t))
    if 0.0 < med < 1.0 and med > MIDTONE_TARGET_L / 100.0:
        g = np.log(MIDTONE_TARGET_L / 100.0) / np.log(med)
        g = min(max(g, 1.0), darken_gamma)
        t = np.power(t, g)
    t = _s_curve(t, CONTRAST_GAIN)
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


def render_mono_bin(
    path: Path,
    *,
    sharpen_radius: float = SHARPEN_RADIUS,
    sharpen_percent: float = SHARPEN_PERCENT,
    sharpen_threshold: int = SHARPEN_THRESHOLD,
    use_measured_ramp: bool = USE_MEASURED_RAMP,
    darken_gamma: float = MAX_DARKEN_GAMMA,
    shadow_lift: float = 0.0,
) -> bytes:
    """Render an original image file to E1003 wire bytes (cached).

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

    st = path.stat()
    key = (
        str(path), st.st_mtime_ns, st.st_size,
        round(sharpen_radius, 3), sharpen_percent, sharpen_threshold,
        use_measured_ramp, round(darken_gamma, 3), round(shadow_lift, 3),
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

    if gray.height > gray.width:
        gray = gray.transpose(PORTRAIT_ROTATE)

    # Fill vs letterbox: cover-crop when the aspect mismatch is small (a
    # sliver of crop beats bars), letterbox on paper-white when large — e.g.
    # a square medium-format frame on the 4:3 panel would lose ~24% of its
    # height to a cover-crop. The white mat reads naturally on e-ink.
    panel_aspect = MONO_W / MONO_H
    img_aspect = gray.width / gray.height
    crop_frac = 1.0 - min(img_aspect, panel_aspect) / max(img_aspect, panel_aspect)
    if crop_frac <= MAX_COVER_CROP_FRAC:
        gray = ImageOps.fit(gray, (MONO_W, MONO_H), Image.Resampling.LANCZOS)
    else:
        gray = ImageOps.contain(gray, (MONO_W, MONO_H), Image.Resampling.LANCZOS)
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
    l_img = _tone_map_lstar(_lstar_from_y(y_img), darken_gamma=darken_gamma)
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


# sRGB value each level *looks like* on the panel (perceived ramp) — previews
# decoded through this match the glass, not the theoretical uniform codes.
_LEVELS_SRGB_PERCEIVED = _linear_to_srgb(_LEVELS_LINEAR)


def mono_bin_to_image(data: bytes) -> Image.Image:
    """Unpack wire bytes to a panel-accurate grayscale preview (tests/preview).

    Levels decode through the PERCEIVED calibrated ramp, so the preview shows
    what the panel will show (compressed blacks/whites included)."""
    if len(data) != MONO_BYTES:
        raise ValueError(f"expected {MONO_BYTES} bytes, got {len(data)}")
    packed = np.frombuffer(data, dtype=np.uint8).reshape(MONO_H, MONO_W // 2)
    idx = np.empty((MONO_H, MONO_W), dtype=np.uint8)
    idx[:, 0::2] = packed >> 4
    idx[:, 1::2] = packed & 0x0F
    return Image.fromarray(_LEVELS_SRGB_PERCEIVED[idx], mode="L")
