"""An editor crop applies only to frames of the shape it was authored for.

The editor records the shape a crop was made for in ``edit_crop["target"]``. On a frame
of the OTHER shape the crop does not apply: the photo renders exactly as if it had never
been cropped — original pixels, same slider, same face-aware framing as any unedited
photo. That is what keeps cropped and uncropped photos from behaving differently, and it
means one crop per photo is enough (the other orientation is handled automatically).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from hokku_server.image_record import ConvertStatus, ImageRecord
from hokku_server.orientation import Orientation

PORTRAIT_CROP = {
    "rotation_quarters": 0,
    "rect": {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0},
    "target": "portrait",
}
LANDSCAPE_CROP = {
    "rotation_quarters": 0,
    "rect": {"x": 0.0, "y": 0.25, "w": 1.0, "h": 0.5},
    "target": "landscape",
}


def _rec(**kw) -> ImageRecord:
    base = dict(
        name="p.jpg",
        name_hash="h",
        original_sha1="s",
        original_size_bytes=1,
        original_mtime=0.0,
        added_at=0.0,
        convert_status=ConvertStatus.OK,
        convert_error=None,
        image_width=1600,
        image_height=1200,
    )
    base.update(kw)
    return ImageRecord(**base)


# ── the rule itself ──────────────────────────────────────────────────────────


def test_crop_applies_to_its_own_shape():
    rec = _rec(edit_crop=PORTRAIT_CROP)
    assert rec.crop_for(Orientation.PORTRAIT) == PORTRAIT_CROP


def test_crop_does_not_apply_to_the_other_shape():
    rec = _rec(edit_crop=PORTRAIT_CROP)
    assert rec.crop_for(Orientation.LANDSCAPE) is None


def test_landscape_crop_is_scoped_the_same_way():
    rec = _rec(edit_crop=LANDSCAPE_CROP)
    assert rec.crop_for(Orientation.LANDSCAPE) == LANDSCAPE_CROP
    assert rec.crop_for(Orientation.PORTRAIT) is None


def test_no_crop_is_none_everywhere():
    rec = _rec()
    assert rec.crop_for(Orientation.LANDSCAPE) is None
    assert rec.crop_for(Orientation.PORTRAIT) is None


def test_legacy_crop_without_a_target_still_applies_everywhere():
    """Crops predating the target field keep their old behaviour rather than silently
    switching off on one orientation."""
    legacy = {"rotation_quarters": 0, "rect": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}
    rec = _rec(edit_crop=legacy)
    assert rec.crop_for(Orientation.LANDSCAPE) == legacy
    assert rec.crop_for(Orientation.PORTRAIT) == legacy


# ── it reaches the render: the off-shape render equals the UNCROPPED render ──


def _render_bytes(rec, orientation, cfg, threshold=0.10):
    """Render through the same helper the manager uses, so this exercises the real path."""
    from dataclasses import replace as dc_replace

    from hokku_server.dither_streaming_numba import NumbaStreamingDither
    from hokku_server.display import FULL_W, PANEL_H
    from hokku_server.image_classifier import ImageClassifierDecision
    from hokku_server.image_manager_abstract import _decision_to_screen_image_config
    from hokku_server.image_renderer import ImageRenderer

    decision = ImageClassifierDecision(
        image_config=cfg, crop_to_fill_threshold=threshold, clahe_keepout_bboxes=None
    )
    scfg = _decision_to_screen_image_config(decision, orientation, rec.crop_for(orientation))
    img = Image.fromarray(_gradient(1600, 1200), "RGB")
    return ImageRenderer(NumbaStreamingDither()).render_indices(
        img, dc_replace(scfg.image_config, dither_noise=0.0), orientation,
        FULL_W, PANEL_H, scfg.crop_to_fill_threshold,
        rotation_quarters=scfg.rotation_quarters, crop_rect=scfg.crop_rect,
    )


def _gradient(w, h):
    a = np.zeros((h, w, 3), np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    a[..., 0] = (xx * 255 // w).astype(np.uint8)
    a[..., 1] = (yy * 255 // h).astype(np.uint8)
    return a


@pytest.fixture
def cfg():
    from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS

    return PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]


def test_off_shape_render_is_identical_to_the_uncropped_photo(cfg):
    """The point of the rule: on a frame the crop was not made for, an edited photo and
    an unedited one produce the SAME pixels — no divergence."""
    cropped = _render_bytes(_rec(edit_crop=PORTRAIT_CROP), Orientation.LANDSCAPE, cfg)
    untouched = _render_bytes(_rec(), Orientation.LANDSCAPE, cfg)
    assert np.array_equal(cropped, untouched)


def test_matching_shape_render_still_uses_the_crop(cfg):
    """…while on its own shape the crop is honoured, so cropping still does something."""
    cropped = _render_bytes(_rec(edit_crop=PORTRAIT_CROP), Orientation.PORTRAIT, cfg)
    untouched = _render_bytes(_rec(), Orientation.PORTRAIT, cfg)
    assert not np.array_equal(cropped, untouched)
