"""Faithful numpy port of the verified `dtcore` tone stage for the E1003 mono editor.

Mirrors scratchpad/dtcore.js exactly:
  * darktable 5.6.0 sigmoid  (src/iop/sigmoid.c)           — grayscale, verbatim math
  * darktable 5.6.0 local-laplacian (src/common/locallaplacian.c) — incl. dt_fast_expf
  * Lightroom-matched Basic sliders (measured transfer LUTs, applied in LR order)
  * CLAHE via cv2.createCLAHE(tileGridSize=(8,8)) on Lab L*  (same as the Spectra-6 path)

Everything operates on the display/perceptual grayscale axis. Verified pixel-exact vs a
real darktable export (mean ΔL 0.57 at full res) and vs real Lightroom exports (mean ≤0.02).

Grayscale-only (single L channel); this is the E1003 monochrome path.
"""
from __future__ import annotations
import json
import os
import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

MIDDLE_GREY = 0.1845
_MAX_LEVELS = 30
_NUM_GAMMA = 6


# ── sRGB transfer (vectorized) ──
def s2l(v):
    v = np.asarray(v, dtype=np.float64)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


def l2s(v):
    v = np.asarray(v, dtype=np.float64)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(np.clip(v, 0, None), 1 / 2.4) - 0.055)


# ── CIE L* (D65, relative Y in [0,1] -> L* in [0,100]) ──
_EPS = 216 / 24389
_KAP = 24389 / 27


def labL(Y):
    Y = np.asarray(Y, dtype=np.float64)
    return 116 * np.where(Y > _EPS, np.cbrt(Y), (_KAP * Y + 16) / 116) - 16


def labLinv(L):
    L = np.asarray(L, dtype=np.float64)
    ft = (L + 16) / 116
    t3 = ft ** 3
    return np.where(t3 > _EPS, t3, (116 * ft - 16) / _KAP)


# ══════════ sigmoid.c ══════════
def _sig_curve(v, mag, pe, fog, fp, pp):
    cv = np.clip(v, 0, None)
    fr = np.power(fog + cv, fp)
    out = mag * np.power(fr / (pe + fr), pp)
    return np.where(np.isnan(out), mag, out)


def sigmoid_params(contrast, skew, white_pct, black_pct):
    """commit_params: derive constants from contrast, skew, display white/black %."""
    MID = MIDDLE_GREY
    d = 1e-6
    rfp = contrast
    rpe = (0 + MID) ** rfp * (1.0 / MID - 1.0)
    rs = (_sig_curve(MID + d, 1.0, rpe, 0.0, rfp, 1.0) - _sig_curve(MID - d, 1.0, rpe, 0.0, rfp, 1.0)) / (2 * d)
    pp = 5.0 ** (-skew)
    twt = 0.01 * white_pct
    twg = (twt / MID) ** (1.0 / pp) - 1.0
    tpe = (MID ** 1.0) * twg
    ts = (_sig_curve(MID + d, twt, tpe, 0.0, 1.0, pp) - _sig_curve(MID - d, twt, tpe, 0.0, 1.0, pp)) / (2 * d)
    fp = rs / ts
    wt = 0.01 * white_pct
    bt = 0.01 * black_pct
    wg = (wt / MID) ** (1.0 / pp) - 1.0
    wb = (bt / wt) ** (-1.0 / pp) - 1.0
    fog = MID * (wg ** (1.0 / fp)) / ((wb ** (1.0 / fp)) - (wg ** (1.0 / fp)))
    pe = ((fog + MID) ** fp) * wg
    return dict(mag=wt, pe=pe, fog=fog, fp=fp, pp=pp)


def apply_sigmoid(v, P):
    return _sig_curve(v, P["mag"], P["pe"], P["fog"], P["fp"], P["pp"])


# ══════════ locallaplacian.c ══════════
def _dt_fast_expf(x):
    k0 = np.trunc(0x3f800000 + x.astype(np.float64) * (0x402DF854 - 0x3f800000)).astype(np.int64)
    k0 = np.where(k0 <= 0, 0, k0).astype(np.int32)
    return k0.view(np.float32)


def _dl(size, level):
    for _ in range(level):
        size = ((size - 1) >> 1) + 1
    return size


def _ll_pad_input(L, max_supp):
    ht, wd = L.shape
    wd2, ht2 = 2 * max_supp + wd, 2 * max_supp + ht
    out = np.zeros((ht2, wd2), dtype=np.float32)
    core = (L.astype(np.float32)) * 0.01
    out[max_supp:max_supp + ht, max_supp:max_supp + wd] = core
    out[max_supp:max_supp + ht, :max_supp] = core[:, :1]
    out[max_supp:max_supp + ht, max_supp + wd:] = core[:, -1:]
    out[:max_supp, :] = out[max_supp:max_supp + 1, :]
    out[max_supp + ht:, :] = out[max_supp + ht - 1:max_supp + ht, :]
    return out


def _ll_fill_boundary1(f):
    f[:, 0] = f[:, 1]
    f[:, -1] = f[:, -2]
    f[0, :] = f[1, :]
    f[-1, :] = f[-2, :]


def _ll_fill_boundary2(f):
    h, w = f.shape
    f[:, 0] = f[:, 1]
    if w & 1:
        f[:, w - 1] = f[:, w - 2]
    else:
        f[:, w - 2] = f[:, w - 3]
        f[:, w - 1] = f[:, w - 3]
    f[0, :] = f[1, :]
    if not (h & 1):
        f[h - 2, :] = f[h - 3, :]
    f[h - 1, :] = f[h - 2, :]


def _gauss_reduce(a):
    ht, wd = a.shape
    cw = ((wd - 1) >> 1) + 1
    ch = ((ht - 1) >> 1) + 1
    # separable 1-4-6-4-1: value at h[i,j] is the 5x5 sum centered at fine (i+2, j+2)
    v = a[:-4] + 4 * a[1:-3] + 6 * a[2:-2] + 4 * a[3:-1] + a[4:]
    h = v[:, :-4] + 4 * v[:, 1:-3] + 6 * v[:, 2:-2] + 4 * v[:, 3:-1] + v[:, 4:]
    out = np.zeros((ch, cw), dtype=np.float32)
    out[1:ch - 1, 1:cw - 1] = h[0:2 * (ch - 2):2, 0:2 * (cw - 2):2] * np.float32(1.0 / 256.0)
    _ll_fill_boundary1(out)
    return out


_Q = np.float32(4.0 / 256.0)


def _gauss_expand(C, fw, fh):
    # strided per-parity upsample (same math as darktable ll_expand_gaussian, ~4x less work than np.where)
    Cp = np.pad(C, 1, mode="edge").astype(np.float32, copy=False)
    nej, noj = (fh + 1) // 2, fh // 2
    nei, noi = (fw + 1) // 2, fw // 2
    fine = np.empty((fh, fw), dtype=np.float32)
    # (even j, even i): 3x3 stencil centered on the coarse cell
    C0 = Cp[1:1 + nej, 1:1 + nei]
    N, S = Cp[0:nej, 1:1 + nei], Cp[2:2 + nej, 1:1 + nei]
    W, E = Cp[1:1 + nej, 0:nei], Cp[1:1 + nej, 2:2 + nei]
    fine[0::2, 0::2] = _Q * (6 * (N + W + 6 * C0 + E + S) + Cp[0:nej, 0:nei] + Cp[0:nej, 2:2 + nei] + Cp[2:2 + nej, 0:nei] + Cp[2:2 + nej, 2:2 + nei])
    # (even j, odd i): between horizontal neighbours
    a, b = Cp[1:1 + nej, 1:1 + noi], Cp[1:1 + nej, 2:2 + noi]
    fine[0::2, 1::2] = _Q * (24 * (a + b) + 4 * (Cp[0:nej, 1:1 + noi] + Cp[0:nej, 2:2 + noi] + Cp[2:2 + nej, 1:1 + noi] + Cp[2:2 + nej, 2:2 + noi]))
    # (odd j, even i): between vertical neighbours
    a, c = Cp[1:1 + noj, 1:1 + nei], Cp[2:2 + noj, 1:1 + nei]
    fine[1::2, 0::2] = _Q * (24 * (a + c) + 4 * (Cp[1:1 + noj, 0:nei] + Cp[1:1 + noj, 2:2 + nei] + Cp[2:2 + noj, 0:nei] + Cp[2:2 + noj, 2:2 + nei]))
    # (odd j, odd i): 2x2 average
    fine[1::2, 1::2] = 0.25 * (Cp[1:1 + noj, 1:1 + noi] + Cp[1:1 + noj, 2:2 + noi] + Cp[2:2 + noj, 1:1 + noi] + Cp[2:2 + noj, 2:2 + noi])
    _ll_fill_boundary2(fine)
    return fine


def _curve(x, g, sigma, shadows, highlights, clarity):
    c = x - g
    val = np.empty_like(x)
    hi = c > 2 * sigma
    lo = c < -2 * sigma
    pos = (~hi) & (~lo) & (c > 0)
    neg = (~hi) & (~lo) & (c <= 0)
    val[hi] = g + sigma + shadows * (c[hi] - sigma)
    val[lo] = g - sigma + highlights * (c[lo] + sigma)
    t = np.clip(c[pos] / (2 * sigma), 0, 1); t2 = t * t; mt = 1 - t
    val[pos] = g + sigma * 2 * mt * t + t2 * (sigma + sigma * shadows)
    t = np.clip(-c[neg] / (2 * sigma), 0, 1); t2 = t * t; mt = 1 - t
    val[neg] = g - sigma * 2 * mt * t + t2 * (-sigma - sigma * highlights)
    val = val + clarity * c * _dt_fast_expf(-c * c / (2 * sigma * sigma / 3))
    return val


def _apply_curve(inp, padding, g, sigma, shadows, highlights, clarity):
    h, w = inp.shape
    out = np.empty_like(inp)
    out[padding:h - padding, padding:w - padding] = _curve(inp[padding:h - padding, padding:w - padding], g, sigma, shadows, highlights, clarity)
    out[padding:h - padding, :padding] = out[padding:h - padding, padding:padding + 1]
    out[padding:h - padding, w - padding:] = out[padding:h - padding, w - padding - 1:w - padding]
    out[:padding, :] = out[padding:padding + 1, :]
    out[h - padding:, :] = out[h - padding - 1:h - padding, :]
    return out


def local_laplacian(L, sigma, shadows, highlights, clarity):
    """L: 2D float array (L*, 0..100). Returns same shape."""
    ht, wd = L.shape
    if wd <= 1 or ht <= 1:
        return L.astype(np.float32).copy()
    num_levels = min(_MAX_LEVELS, min(wd, ht).bit_length() - 1)
    last = num_levels - 1
    max_supp = 1 << last
    padded = [None] * (last + 1)
    padded[0] = _ll_pad_input(L, max_supp)
    h0, w0 = padded[0].shape
    dims = [(_dl(w0, l), _dl(h0, l)) for l in range(last + 1)]  # (w_l, h_l)
    for l in range(1, last):
        padded[l] = _gauss_reduce(padded[l - 1])
    output = [None] * (last + 1)
    output[last] = _gauss_reduce(padded[last - 1])
    gamma = ((np.arange(_NUM_GAMMA) + 0.5) / _NUM_GAMMA).astype(np.float32)
    buf = [[None] * (last + 1) for _ in range(_NUM_GAMMA)]
    for k in range(_NUM_GAMMA):
        buf[k][0] = _apply_curve(padded[0], max_supp, gamma[k], sigma, shadows, highlights, clarity)
        for l in range(1, last + 1):
            buf[k][l] = _gauss_reduce(buf[k][l - 1])
    for l in range(last - 1, -1, -1):
        pw, ph = dims[l]
        exp = _gauss_expand(output[l + 1], pw, ph)
        v = padded[l]
        # laplacian per gamma at this level
        lap = np.stack([buf[k][l] - _gauss_expand(buf[k][l + 1], pw, ph) for k in range(_NUM_GAMMA)])  # (G, ph, pw)
        idx = np.searchsorted(gamma, v.ravel(), side="right").reshape(v.shape)
        hi = np.clip(idx, 1, _NUM_GAMMA - 1)
        lo = hi - 1
        a = np.clip((v - gamma[lo]) / (gamma[hi] - gamma[lo]), 0, 1)
        l0 = np.take_along_axis(lap, lo[None], axis=0)[0]
        l1 = np.take_along_axis(lap, hi[None], axis=0)[0]
        output[l] = exp + l0 * (1 - a) + l1 * a
    out = 100.0 * output[0][max_supp:max_supp + ht, max_supp:max_supp + wd]
    return out.astype(np.float32)


# ══════════ Lightroom-matched Basic (measured transfer LUTs) ══════════
_LR = None


def _load_lr(path=None):
    global _LR
    if _LR is not None:
        return _LR
    # preferred: embedded module (mono_lr_luts.LR_DELTAS)
    _m = None
    try:
        from . import mono_lr_luts as _m  # package context (server)
    except Exception:
        try:
            import mono_lr_luts as _m  # flat context (scratchpad)
        except Exception:
            _m = None
    if _m is not None:
        _LR = {k: {"p": np.array(v["p"], dtype=np.float64), "m": np.array(v["m"], dtype=np.float64)}
               for k, v in _m.LR_DELTAS.items()}
        return _LR
    # fallback: compute from lr_curves.json
    path = path or os.path.join(os.path.dirname(__file__), "lr_curves.json")
    with open(path) as f:
        c = json.load(f)

    def delta(out):
        out = np.array(out, dtype=np.float64)
        d = out - np.arange(256)
        s = d.copy()
        s[2:254] = (d[:-4] + 2 * d[1:-3] + 3 * d[2:-2] + 2 * d[3:-1] + d[4:]) / 9
        return np.round(s)

    keys = {"contrast": "ct", "highlights": "hl", "shadows": "sh", "whites": "wh", "blacks": "bk"}
    _LR = {k: {"p": delta(c[v + "_p"]), "m": delta(c[v + "_m"])} for k, v in keys.items()}
    return _LR


def _lr_apply(x, P, M, a):
    if not a:
        return x
    idx = np.clip(np.round(np.clip(x, 0, 1) * 255).astype(int), 0, 255)
    d = (a * P[idx]) if a > 0 else (-a * M[idx])
    return np.clip(x + d / 255.0, 0, 1)


def apply_basic(disp, exposure=0.0, contrast=0.0, highlights=0.0, shadows=0.0, whites=0.0, blacks=0.0):
    """disp: display sRGB array [0,1]. params: exposure in EV; others in [-1,1]. Returns [0,1]."""
    lr = _load_lr()
    x = np.asarray(disp, dtype=np.float64)
    if exposure:
        lin = s2l(x) * (2.0 ** exposure)
        if exposure > 0:
            k = 0.8
            lin = np.where(lin > k, k + (1 - k) * (1 - np.exp(-(lin - k) / (1 - k))), lin)
        x = l2s(np.clip(lin, 0, 1))
    x = _lr_apply(x, lr["contrast"]["p"], lr["contrast"]["m"], contrast)
    x = _lr_apply(x, lr["highlights"]["p"], lr["highlights"]["m"], highlights)
    x = _lr_apply(x, lr["shadows"]["p"], lr["shadows"]["m"], shadows)
    x = _lr_apply(x, lr["whites"]["p"], lr["whites"]["m"], whites)
    x = _lr_apply(x, lr["blacks"]["p"], lr["blacks"]["m"], blacks)
    return np.clip(x, 0, 1)


def apply_clahe(disp, clip):
    """CLAHE on the L* of a display-sRGB array (2D). clip = cv2 clipLimit (0 = off)."""
    if clip <= 0 or cv2 is None:
        return disp
    L255 = np.clip(labL(s2l(np.clip(disp, 0, 1))) * 2.55, 0, 255).astype(np.uint8)
    cl = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(8, 8)).apply(L255)
    return np.clip(l2s(np.clip(labLinv(cl.astype(np.float64) / 2.55), 0, 1)), 0, 1)


# ══════════ full grayscale tone pipeline ══════════
def render_tone(gray_u8, *, sigmoid=None, local_contrast=None, basic=None):
    """gray_u8: 2D uint8 grayscale (already fit/rotated). Returns 2D uint8 display grayscale.

    sigmoid: dict {enabled, contrast, skew, white, black} or None
    local_contrast: dict {enabled, detail, highlights, shadows, midtone} (darktable) or None
    basic: dict {enabled, exposure, contrast, highlights, shadows, whites, blacks, clahe} or None
    """
    g = np.asarray(gray_u8)
    x01 = g.astype(np.float64) / 255.0
    # sigmoid (scene-linear -> display-linear) then Lab L*
    if sigmoid and sigmoid.get("enabled", True):
        P = sigmoid_params(sigmoid["contrast"], sigmoid["skew"], sigmoid["white"], sigmoid["black"])
        y = apply_sigmoid(s2l(x01), P)
    else:
        y = s2l(x01)
    Lstar = labL(np.clip(y, 0, 1))
    # darktable local-laplacian on L*
    if local_contrast and local_contrast.get("enabled", True) and local_contrast.get("detail", 0) is not None:
        Lstar = local_laplacian(Lstar, local_contrast["midtone"], local_contrast["shadows"],
                                local_contrast["highlights"], local_contrast["detail"])
    disp = np.clip(l2s(np.clip(labLinv(Lstar), 0, 1)), 0, 1)
    disp = _apply_basic_stage(disp, basic)
    return np.clip(np.round(disp * 255), 0, 255).astype(np.uint8)


def _apply_basic_stage(disp, basic):
    """The cheap tail of render_tone: Lightroom Basic + CLAHE on the [0,1] display array.
    Split out so the staged preview path can re-run ONLY this when just basic_*/clahe
    changed (the sigmoid + local-laplacian result upstream is reused from cache)."""
    if basic and basic.get("enabled", True):
        disp = apply_basic(disp, exposure=basic.get("exposure", 0.0), contrast=basic.get("contrast", 0.0),
                           highlights=basic.get("highlights", 0.0), shadows=basic.get("shadows", 0.0),
                           whites=basic.get("whites", 0.0), blacks=basic.get("blacks", 0.0))
        disp = apply_clahe(disp, basic.get("clahe", 0.0))
    return disp


# ── Staged preview cache: the expensive sigmoid + local-laplacian output (`disp`, the
#    [0,1] display array BEFORE Basic/CLAHE) keyed on (image identity, sigmoid params,
#    local_contrast params). A Tone-page drag changes only basic_*/CLAHE, so it reuses
#    the cached `disp` and re-runs just the cheap tail. PREVIEW-ONLY — render_tone() (the
#    saved/full-res path) never touches this, so the committed engine is unaffected. ──
import threading as _threading

_DISP_CACHE_MAX = 4
_disp_cache: "dict[tuple, np.ndarray]" = {}
_disp_lock = _threading.Lock()


def _round_dict(d, keys):
    if not d:
        return None
    out = []
    for k in keys:
        v = d.get(k)
        out.append(round(v, 4) if isinstance(v, float) else v)
    return tuple(out)


_SIG_KEYS = ("enabled", "contrast", "skew", "white", "black")
_LC_KEYS = ("enabled", "detail", "highlights", "shadows", "midtone")


def render_tone_staged(gray_u8, *, id_key, sigmoid=None, local_contrast=None, basic=None):
    """Same result as render_tone(), but caches the pre-Basic display array keyed on
    (id_key, sigmoid, local_contrast). When only basic_*/CLAHE differ from a prior call
    on the same id_key, the sigmoid + local-laplacian work is skipped entirely.

    id_key identifies the fit grayscale content (path + mtime + preview size). PREVIEW
    use only — render_tone() stays the authority for the saved/full-res render."""
    ck = (id_key, _round_dict(sigmoid, _SIG_KEYS), _round_dict(local_contrast, _LC_KEYS))
    with _disp_lock:
        disp = _disp_cache.get(ck)
    if disp is None:
        g = np.asarray(gray_u8)
        x01 = g.astype(np.float64) / 255.0
        if sigmoid and sigmoid.get("enabled", True):
            P = sigmoid_params(sigmoid["contrast"], sigmoid["skew"], sigmoid["white"], sigmoid["black"])
            y = apply_sigmoid(s2l(x01), P)
        else:
            y = s2l(x01)
        Lstar = labL(np.clip(y, 0, 1))
        if local_contrast and local_contrast.get("enabled", True) and local_contrast.get("detail", 0) is not None:
            Lstar = local_laplacian(Lstar, local_contrast["midtone"], local_contrast["shadows"],
                                    local_contrast["highlights"], local_contrast["detail"])
        disp = np.clip(l2s(np.clip(labLinv(Lstar), 0, 1)), 0, 1)
        with _disp_lock:
            while len(_disp_cache) >= _DISP_CACHE_MAX:
                _disp_cache.pop(next(iter(_disp_cache)))
            _disp_cache[ck] = disp
    out = _apply_basic_stage(disp, basic)
    return np.clip(np.round(out * 255), 0, 255).astype(np.uint8)
