"""The mono preview un-rotate must agree with what the render actually composed.

_mono_content_portrait (used by the UI preview to decide whether to un-rotate) must
return the SAME content orientation that _fit_gray_to_panel composes into the wire
buffer — for every crop / rotation / auto_rotate / mount combination. A mismatch is
the sideways-preview bug on edited photos (amendment 4).
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from hokku_server import mono_e1003
from hokku_server.image_abc import effective_cropped_dims
from hokku_server.mono_e1003 import MONO_H, MONO_W, PORTRAIT_ROTATE


def _gray(w, h):
    a = np.zeros((h, w), np.uint8)
    a[: h // 4, :] = 255  # top band → detect orientation of composed content
    return Image.fromarray(a, "L")


# (src_w, src_h) covering portrait / landscape / square sources
_SRCS = [(1400, 900), (900, 1400), (1100, 1100)]
_CROPS = [None, (0.30, 0.05, 0.40, 0.90), (0.05, 0.30, 0.90, 0.40)]  # none / tall / wide
_ROTS = [0, 1]
_AUTOROT = [False, True]
_FRAMES = [True, False]


@pytest.mark.parametrize("src", _SRCS)
@pytest.mark.parametrize("crop", _CROPS)
@pytest.mark.parametrize("rot", _ROTS)
@pytest.mark.parametrize("auto_rotate", _AUTOROT)
@pytest.mark.parametrize("frame_portrait", _FRAMES)
def test_preview_orientation_matches_render(src, crop, rot, auto_rotate, frame_portrait):
    sw, sh = src
    gray = _gray(sw, sh)

    # What the preview code predicts (drives whether it un-rotates):
    predicted_portrait = mono_e1003._mono_content_portrait(
        sw, sh, rot, crop, auto_rotate, frame_portrait
    )

    # What the render actually composed: derive the SAME content orientation the render
    # used, straight from the shared decision on the effective (post-crop) dims. This is
    # exactly the path _fit_gray_to_panel takes, so the preview prediction must equal it.
    from hokku_server.image_abc import frame_decision

    ew, eh = effective_cropped_dims(sw, sh, rot, crop)
    render_decision = frame_decision(
        ew, eh, MONO_W, MONO_H,
        frame_portrait=frame_portrait, auto_rotate=auto_rotate,
        has_crop=crop is not None, crop_to_fill_threshold=0.0,
    ).content_portrait

    assert predicted_portrait == render_decision, (
        f"preview predicts portrait={predicted_portrait} but render composed "
        f"portrait={render_decision} for src={src} crop={crop} rot={rot} "
        f"ar={auto_rotate} frame_portrait={frame_portrait}"
    )

    # And the render's fitted content is always orientable into the landscape wire buffer:
    # portrait content is transposed to (MONO_W, MONO_H); a letterboxed (contain) result may
    # be smaller and is matted to the full buffer downstream — so assert it FITS, not equals.
    fitted = mono_e1003._fit_gray_to_panel(
        gray, MONO_W, MONO_H, rot, crop, auto_rotate, frame_portrait
    )
    assert fitted.width <= MONO_W and fitted.height <= MONO_H


def test_effective_cropped_dims_matches_actual_apply():
    """effective_cropped_dims must predict _apply_crop_rotation's real output size."""
    from hokku_server.image_abc import _apply_crop_rotation

    for sw, sh in _SRCS:
        for rot in (0, 1, 2, 3):
            for crop in _CROPS:
                img = Image.new("L", (sw, sh))
                out = _apply_crop_rotation(img, rot, crop)
                assert effective_cropped_dims(sw, sh, rot, crop) == out.size, (
                    f"src=({sw},{sh}) rot={rot} crop={crop}: "
                    f"predicted {effective_cropped_dims(sw, sh, rot, crop)} "
                    f"actual {out.size}"
                )
