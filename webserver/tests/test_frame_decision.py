"""Unit tests for frame_decision() — the shared color/mono framing policy (UNIFY_PLAN.md).

Pure scalar function, so the full D1-D4 truth table is exercised directly with no
images. Both pipelines consume this; if these pass, both frame identically.

Panel dims used: color-like 1200x1600 (portrait panel). The content canvas takes the
content orientation regardless of arg order.
"""

from __future__ import annotations

import pytest

from hokku_server.image_abc import FramingDecision, frame_decision

PW, PH = 1200, 1600  # portrait-shaped panel (either arg order is accepted)


def _decide(src_w, src_h, *, frame_portrait, auto_rotate, has_crop, thr=0.0):
    return frame_decision(
        src_w, src_h, PW, PH,
        frame_portrait=frame_portrait, auto_rotate=auto_rotate,
        has_crop=has_crop, crop_to_fill_threshold=thr,
    )


# ── D1: content orientation ─────────────────────────────────────────────────────
# OFF follows the SOURCE shape; ON follows the FRAME.

@pytest.mark.parametrize("src_w,src_h,src_is_portrait", [(900, 1400, True), (1400, 900, False), (1100, 1100, False)])
@pytest.mark.parametrize("frame_portrait", [True, False])
def test_d1_off_follows_source(src_w, src_h, src_is_portrait, frame_portrait):
    d = _decide(src_w, src_h, frame_portrait=frame_portrait, auto_rotate=False, has_crop=False, thr=1.0)
    assert d.content_portrait == src_is_portrait  # frame orientation irrelevant when OFF


@pytest.mark.parametrize("src_w,src_h", [(900, 1400), (1400, 900), (1100, 1100)])
@pytest.mark.parametrize("frame_portrait", [True, False])
def test_d1_on_follows_frame(src_w, src_h, frame_portrait):
    d = _decide(src_w, src_h, frame_portrait=frame_portrait, auto_rotate=True, has_crop=False)
    assert d.content_portrait == frame_portrait  # source orientation irrelevant when ON


# ── D1: manual crop is not a special case — a portrait crop stays portrait ───────
# (caller passes the ALREADY-cropped dims; a portrait crop => src_h > src_w)

def test_d1_portrait_crop_stays_portrait_off():
    # landscape original cropped to a tall portrait shape → dims are portrait
    d = _decide(600, 1500, frame_portrait=False, auto_rotate=False, has_crop=True)
    assert d.content_portrait is True   # honoured, NOT flattened to the landscape frame
    assert d.use_cover is True          # a crop always fills (D3)


def test_d1_landscape_crop_stays_landscape_off():
    d = _decide(1500, 600, frame_portrait=True, auto_rotate=False, has_crop=True)
    assert d.content_portrait is False
    assert d.use_cover is True


# ── D4/B1: with a crop, auto_rotate STILL wins the orientation ───────────────────

def test_d4_b1_autorotate_overrides_crop_orientation():
    # portrait crop (600x1500) on a LANDSCAPE mount, auto_rotate ON → reframed to landscape
    d = _decide(600, 1500, frame_portrait=False, auto_rotate=True, has_crop=True)
    assert d.content_portrait is False  # auto_rotate wins → landscape content (B1)
    assert d.use_cover is True


def test_d4_crop_off_vs_on_differ_on_mismatched_mount():
    off = _decide(600, 1500, frame_portrait=False, auto_rotate=False, has_crop=True)
    on = _decide(600, 1500, frame_portrait=False, auto_rotate=True, has_crop=True)
    assert off.content_portrait is True   # OFF honours the crop shape
    assert on.content_portrait is False   # ON reframes to the mount
    assert off.use_cover and on.use_cover  # both fill


# ── D3: crop OR auto_rotate force cover unconditionally, threshold irrelevant ────

@pytest.mark.parametrize("has_crop,auto_rotate", [(True, False), (False, True), (True, True)])
def test_d3_explicit_fills_ignore_threshold(has_crop, auto_rotate):
    # even with threshold 0 (which would letterbox a plain photo), these force cover
    d = _decide(1400, 900, frame_portrait=True, auto_rotate=auto_rotate, has_crop=has_crop, thr=0.0)
    assert d.use_cover is True


# ── D2: plain case (no crop, no auto_rotate) — the single slider governs fill ────

def test_d2_threshold_zero_letterboxes_mismatched_aspect():
    # landscape photo, landscape content (OFF), but panel is portrait-shaped → needs zoom
    d = _decide(1400, 900, frame_portrait=False, auto_rotate=False, has_crop=False, thr=0.0)
    assert d.use_cover is False  # 0% threshold → never auto-fill


def test_d2_high_threshold_fills():
    d = _decide(1400, 900, frame_portrait=False, auto_rotate=False, has_crop=False, thr=1.0)
    assert d.use_cover is True   # generous threshold → fill


def test_d2_threshold_is_the_only_knob_boundary():
    # find a case with a known zoom ratio and verify the threshold boundary governs it.
    # landscape source 1600x900 on landscape content canvas (long=1600, short=1200):
    #   fit = min(1600/1600, 1200/900)=1.0 ; cover = max(...)=1.333 ; ratio≈0.333
    src_w, src_h = 1600, 900
    below = _decide(src_w, src_h, frame_portrait=False, auto_rotate=False, has_crop=False, thr=0.30)
    above = _decide(src_w, src_h, frame_portrait=False, auto_rotate=False, has_crop=False, thr=0.40)
    assert below.use_cover is False  # 0.333 > 0.30 → letterbox
    assert above.use_cover is True   # 0.333 <= 0.40 → fill


# ── sanity: return type ─────────────────────────────────────────────────────────

def test_returns_framing_decision():
    d = _decide(1000, 1000, frame_portrait=True, auto_rotate=False, has_crop=False)
    assert isinstance(d, FramingDecision)
