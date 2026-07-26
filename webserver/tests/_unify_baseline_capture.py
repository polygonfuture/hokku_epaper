"""Shared fixture generation for the unify regression guard.

Deterministic synthetic photos + the full framing matrix, rendered through both
pipelines. Imported by both the one-time baseline capture (run pre-refactor to write
the snapshot) and the regression test (compares current output to the snapshot).

Determinism: color uses dither_noise=0.0 (Floyd-Steinberg noise would otherwise
randomize the output bytes). Mono is deterministic already.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

from hokku_server import mono_e1003
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_renderer import ImageRenderer
from hokku_server.orientation import Orientation
from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS

PANEL_W, PANEL_H = 1200, 1600
THRESHOLD = 0.54

EDITS = {
    "none": dict(rotation_quarters=0, crop_rect=None),
    "crop": dict(rotation_quarters=0, crop_rect=(0.30, 0.05, 0.40, 0.90)),
    "rot": dict(rotation_quarters=1, crop_rect=None),
    "croprot": dict(rotation_quarters=1, crop_rect=(0.10, 0.20, 0.80, 0.55)),
}
PHOTOS = ["portrait", "landscape", "square"]
FRAMES = [("portrait", True), ("landscape", False)]
AUTOROT = [False, True]


def neutral_cfg():
    return replace(
        PRESET_IMAGE_CONFIGS[FALLBACK_PRESET],
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        prepare_usm_amount=0,
        color_enhance=1.0,
        adaptive_saturate_space="off",
        dither_noise=0.0,
    )


def make_photo(kind: str) -> Image.Image:
    if kind == "portrait":
        w, h = 900, 1400
    elif kind == "landscape":
        w, h = 1400, 900
    else:
        w, h = 1100, 1100
    a = np.zeros((h, w, 3), np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    a[..., 0] = (xx * 255 // w).astype(np.uint8)
    a[..., 1] = (yy * 255 // h).astype(np.uint8)
    a[..., 2] = ((xx + yy) * 255 // (w + h)).astype(np.uint8)
    a[: h // 8, :] = (0, 0, 0)
    a[:, : w // 8] = (255, 255, 255)
    return Image.fromarray(a, "RGB")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def capture_all() -> dict[str, str]:
    """Return {case_key: sha16} for every (pipeline, photo, frame, auto_rotate, edit)."""
    out: dict[str, str] = {}
    renderer = ImageRenderer(NumbaStreamingDither())
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for photo in PHOTOS:
            png = tmp / f"{photo}.png"
            make_photo(photo).save(png)
            for fname, fportrait in FRAMES:
                orient = Orientation.PORTRAIT if fportrait else Orientation.LANDSCAPE
                for ar in AUTOROT:
                    for ename, ekw in EDITS.items():
                        with Image.open(png) as im:
                            arr = renderer.render_indices(
                                im.convert("RGB"), neutral_cfg(), orient,
                                PANEL_W, PANEL_H, THRESHOLD, auto_rotate=ar, **ekw,
                            )
                        out[f"color|{photo}|{fname}|ar={ar}|{ename}"] = _sha(arr.tobytes())
                        mono_e1003._cache.clear()
                        b = mono_e1003.render_mono_bin(
                            png, auto_rotate=ar, frame_portrait=fportrait, **ekw,
                        )
                        out[f"mono|{photo}|{fname}|ar={ar}|{ename}"] = _sha(b)
    return out


# Cases the unify refactor is EXPECTED to change (must NOT be frozen):
#  - mono crop/rot/croprot: today they ignore frame + auto_rotate (the D1 bug)
# Everything else is frozen: all color, and mono no-edit.
def is_frozen(key: str) -> bool:
    if key.startswith("mono|") and not key.endswith("|none"):
        return False
    return True
