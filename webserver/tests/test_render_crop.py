"""Phase 0 render seams: manual crop + rotation in _prepare_canvas and on ScreenImageConfig.

These prove the editor's crop/rotation actually selects the right pixels and that the
cache slug only moves when a crop/rotation is set (so non-edited images never re-render).
"""

from __future__ import annotations

from dataclasses import asdict, replace

import numpy as np
from PIL import Image

from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import _apply_crop_rotation, _rotate_norm_bbox_cw
from hokku_server.image_config import ImageConfig
from hokku_server.image_renderer import ImageRenderer
from hokku_server.orientation import Orientation
from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS
from hokku_server.screen_image_config import ScreenImageConfig, _screen_image_config_from_dict


def _neutral_cfg() -> ImageConfig:
    """A near pass-through ImageConfig so _prepare_canvas keeps colours recognisable."""
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
    )


def _prep(img: Image.Image, **kw) -> np.ndarray:
    renderer = ImageRenderer(NumbaStreamingDither())
    arr, _mask = renderer._prepare_canvas(img, _neutral_cfg(), Orientation.LANDSCAPE, 400, 300, **kw)
    return arr


def _half_red_blue(w: int = 800, h: int = 600) -> Image.Image:
    a = np.zeros((h, w, 3), np.uint8)
    a[:, : w // 2] = (220, 40, 40)   # left half red
    a[:, w // 2 :] = (40, 40, 220)   # right half blue
    return Image.fromarray(a, "RGB")


def _redness(arr: np.ndarray) -> float:
    return float(arr[..., 0].mean()) - float(arr[..., 2].mean())


def test_apply_crop_rotation_selects_the_region():
    img = _half_red_blue()
    left = np.asarray(_apply_crop_rotation(img, 0, (0.0, 0.0, 0.5, 1.0)))   # red half
    right = np.asarray(_apply_crop_rotation(img, 0, (0.5, 0.0, 0.5, 1.0)))  # blue half
    assert left.shape[1] == 400 and right.shape[1] == 400  # each half is 400 px wide
    assert _redness(left) > 100
    assert _redness(right) < -100


def test_apply_crop_rotation_rotates_dimensions():
    img = _half_red_blue(800, 600)
    assert _apply_crop_rotation(img, 1, None).size == (600, 800)  # a quarter turn swaps w/h
    assert _apply_crop_rotation(img, 2, None).size == (800, 600)  # half turn keeps them


def test_crop_flows_through_prepare_canvas():
    assert not np.array_equal(
        _prep(_half_red_blue()),
        _prep(_half_red_blue(), crop_rect=(0.0, 0.0, 0.5, 1.0)),
    )


def test_rotation_quarters_changes_the_canvas():
    a0 = _prep(_half_red_blue())
    a1 = _prep(_half_red_blue(), rotation_quarters=1)
    a2 = _prep(_half_red_blue(), rotation_quarters=2)
    assert not np.array_equal(a0, a1)
    assert not np.array_equal(a1, a2)


def test_defaults_render_identically_to_no_kwargs():
    assert np.array_equal(
        _prep(_half_red_blue()),
        _prep(_half_red_blue(), rotation_quarters=0, crop_rect=None),
    )


def test_rotate_norm_bbox_cw_moves_top_left_to_top_right():
    # a small box in the top-left corner, rotated one clockwise quarter, lands top-right
    x, y, w, h = _rotate_norm_bbox_cw(0.0, 0.0, 0.2, 0.1)
    assert (round(x, 3), round(y, 3), round(w, 3), round(h, 3)) == (0.9, 0.0, 0.1, 0.2)


def test_cache_slug_only_moves_when_crop_or_rotation_set():
    base = ScreenImageConfig(image_config=_neutral_cfg(), orientation=Orientation.LANDSCAPE)
    same = ScreenImageConfig(
        image_config=_neutral_cfg(),
        orientation=Orientation.LANDSCAPE,
        rotation_quarters=0,
        crop_rect=None,
    )
    assert base.cache_slug() == same.cache_slug()  # non-edited images keep their slug

    cropped = ScreenImageConfig(
        image_config=_neutral_cfg(), orientation=Orientation.LANDSCAPE, crop_rect=(0.1, 0.1, 0.8, 0.8)
    )
    rotated = ScreenImageConfig(
        image_config=_neutral_cfg(), orientation=Orientation.LANDSCAPE, rotation_quarters=1
    )
    assert len({base.cache_slug(), cropped.cache_slug(), rotated.cache_slug()}) == 3


def test_screen_image_config_round_trips_crop_and_rotation():
    d = {
        "image_config": asdict(_neutral_cfg()),
        "orientation": "portrait",
        "rotation_quarters": 3,
        "crop_rect": [0.1, 0.2, 0.5, 0.6],
    }
    back = _screen_image_config_from_dict(d)
    assert back.rotation_quarters == 3
    assert back.crop_rect == (0.1, 0.2, 0.5, 0.6)
