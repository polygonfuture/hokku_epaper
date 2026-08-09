"""Face-aware cropping: a render that must FILL aims its crop window at the faces
instead of the image centre, so the background is lost rather than someone's head.

``_face_centered_crop_offset`` is ported verbatim from upstream (defl/hokku_epaper 4.0.x)
so the eventual v4 port is a no-op for this feature — these tests pin the formula and the
clamps. The integration tests then prove the offset actually reaches both pipelines and
rides through a manual crop.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from hokku_server.bounding_box import BoundingBox
from hokku_server.display import FULL_W, PANEL_H
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import _face_centered_crop_offset
from hokku_server.image_renderer import ImageRenderer
from hokku_server.presets import FALLBACK_PRESET, PRESET_IMAGE_CONFIGS


def _flat_cfg():
    """Deterministic, tone-neutral pipeline: dither_noise=0 (the preset default is
    random, which would make two renders of the same image differ), and no tone
    shaping, so pure white stays white ink and pure black stays black ink."""
    base = PRESET_IMAGE_CONFIGS[FALLBACK_PRESET]
    return replace(
        base,
        dither_noise=0.0,
        prepare_autocontrast_cutoff=0.0,
        prepare_gamma=1.0,
        prepare_brightness=1.0,
        prepare_contrast=1.0,
        prepare_midtone=1.0,
        clahe_clip_limit=0.0,
        prepare_usm_amount=0,
        color_enhance=1.0,
        adaptive_saturate_space="off",
    )


# ── the ported formula ───────────────────────────────────────────────────────


def test_centres_the_face_union_when_there_is_room():
    """Interior (unclamped) offset lands at exactly face_centre - visible/2."""
    bbox = BoundingBox(x=0.4, y=0.4, w=0.2, h=0.2)   # union centre = 0.5, 0.5
    x_off, y_off = _face_centered_crop_offset((bbox,), 400.0, 400.0, 100.0, 100.0)
    assert x_off == pytest.approx(150.0)   # 0.5*400 - 50
    assert y_off == pytest.approx(150.0)


def test_clamps_at_the_top_edge():
    """A face near the top cannot pull the window off the image."""
    bbox = BoundingBox(x=0.0, y=0.0, w=0.1, h=0.1)
    _, y_off = _face_centered_crop_offset((bbox,), 100.0, 400.0, 100.0, 100.0)
    assert y_off == 0.0


def test_clamps_at_the_bottom_edge():
    bbox = BoundingBox(x=0.0, y=0.95, w=0.05, h=0.05)
    _, y_off = _face_centered_crop_offset((bbox,), 100.0, 400.0, 100.0, 100.0)
    assert y_off == 300.0   # scaled_h - visible_h


def test_uses_the_union_of_every_face():
    """Two faces → the window centres on their combined box, not on either one."""
    a = BoundingBox(x=0.0, y=0.10, w=0.05, h=0.05)
    b = BoundingBox(x=0.0, y=0.50, w=0.05, h=0.05)
    _, y_off = _face_centered_crop_offset((a, b), 100.0, 400.0, 100.0, 100.0)
    union_centre = (0.10 + 0.55) / 2 * 400.0
    assert y_off == pytest.approx(union_centre - 50.0)


def test_no_slack_means_no_movement():
    """When the scaled image is exactly the window, the offset can only be 0."""
    bbox = BoundingBox(x=0.9, y=0.9, w=0.05, h=0.05)
    x_off, y_off = _face_centered_crop_offset((bbox,), 100.0, 100.0, 100.0, 100.0)
    assert (x_off, y_off) == (0.0, 0.0)


# ── it reaches the real render ───────────────────────────────────────────────


def _photo_with_marker_at_top(w, h):
    """WHITE photo with a black band across the TOP eighth — stands in for a face.
    White renders as white ink and black as black ink, so counting black pixels is an
    exact measure of whether the marker survived the crop."""
    a = np.full((h, w, 3), 255, np.uint8)
    a[: h // 8, :] = 0
    return Image.fromarray(a, "RGB")


def _render(img, orientation, threshold, anchor):
    return ImageRenderer(NumbaStreamingDither()).render_indices(
        img, _flat_cfg(), orientation,
        FULL_W, PANEL_H, threshold,
        crop_anchor_bboxes_norm=anchor,
    )


def test_face_at_the_top_survives_a_fill_that_centre_crop_would_discard():
    """A tall photo cover-cropped to a landscape frame: centred, the top band is cut
    away; anchored on a face in that band, it survives.

    3:2 portrait source → needs exactly 100% zoom to fill a 4:3 landscape frame, so a
    threshold of 1.0 covers it (a 2:1 source would need 167% and letterbox instead,
    where the anchor is ignored by design).
    """
    src = _photo_with_marker_at_top(1200, 1800)
    face = (BoundingBox(x=0.4, y=0.01, w=0.2, h=0.10),)   # inside the black band

    centred = _render(src.copy(), "landscape", 1.0, None)
    anchored = _render(src.copy(), "landscape", 1.0, face)

    # palette index 0 is black ink; the marker should be absent when centred and
    # present when the window is aimed at it.
    assert (centred == 0).mean() < 0.02, "centre crop should have discarded the top band"
    assert (anchored == 0).mean() > 0.10, "face-anchored crop must keep the marker"


def test_anchor_is_ignored_while_letterboxing():
    """Face-aware only moves a COVER window; a letterboxed render is untouched."""
    src = _photo_with_marker_at_top(1200, 1800)
    face = (BoundingBox(x=0.4, y=0.01, w=0.2, h=0.10),)
    plain = _render(src.copy(), "landscape", 0.0, None)
    anchored = _render(src.copy(), "landscape", 0.0, face)
    assert np.array_equal(plain, anchored)


def test_anchor_rides_through_a_manual_crop():
    """The anchor is in ORIGINAL coords, so it must be transformed by the editor crop —
    otherwise a cropped photo aims at where the face used to be."""
    src = _photo_with_marker_at_top(1200, 1800)
    # keep the top 60% (the marker stays, at the top of the cropped image)
    crop_rect = (0.0, 0.0, 1.0, 0.6)
    face = (BoundingBox(x=0.4, y=0.01, w=0.2, h=0.06),)
    out = ImageRenderer(NumbaStreamingDither()).render_indices(
        src, _flat_cfg(), "landscape",
        FULL_W, PANEL_H, 1.0,
        crop_rect=crop_rect,
        crop_anchor_bboxes_norm=face,
    )
    assert (out == 0).mean() > 0.10, "marker must survive: anchor followed the crop"
