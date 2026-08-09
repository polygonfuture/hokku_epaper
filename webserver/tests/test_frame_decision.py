"""Unit tests for frame_decision() — the shared color/mono framing policy.

Pure scalar function, so the full truth table is exercised directly with no images.
Both pipelines consume this; if these pass, both frame identically.

The policy is two rules and no special cases:
  1. Content orientation ALWAYS follows the FRAME — every photo is composed upright for
     the panel's configured orientation, whatever its own shape. Nothing is ever laid
     sideways, and there is no toggle.
  2. ``crop_to_fill_threshold`` alone decides letterbox vs cover-fill. A manual editor
     crop is NOT a special case: it simply supplies different pixels, and the same
     threshold governs them. (Editor crops are cut to the panel aspect, so on a matching
     frame they need ~0% zoom; ``_EDGE_SNAP_PX`` in the render covers the last pixel.)

Panel dims used: color-like 1200x1600 (portrait panel). The content canvas takes the
content orientation regardless of arg order.
"""

from __future__ import annotations

import pytest

from hokku_server.image_abc import FramingDecision, frame_decision

PW, PH = 1200, 1600  # portrait-shaped panel (either arg order is accepted)


def _decide(src_w, src_h, *, frame_portrait, thr=0.0):
    return frame_decision(
        src_w, src_h, PW, PH,
        frame_portrait=frame_portrait,
        crop_to_fill_threshold=thr,
    )


# ── content orientation: always the frame's, never the photo's ──────────────────

@pytest.mark.parametrize("src_w,src_h", [(900, 1400), (1400, 900), (1100, 1100)])
@pytest.mark.parametrize("frame_portrait", [True, False])
def test_content_always_follows_frame(src_w, src_h, frame_portrait):
    d = _decide(src_w, src_h, frame_portrait=frame_portrait, thr=1.0)
    assert d.content_portrait == frame_portrait  # source orientation never overrides


# ── no sideways: a mismatched photo letterboxes upright unless the slider fills ──

def test_mismatched_photo_letterboxes_upright_at_sane_threshold():
    # panel-shaped portrait photo (1200x1600) on a LANDSCAPE frame: composed landscape
    # (upright); the 90° mismatch needs ~78% zoom, far past any sane threshold → letterbox.
    d = _decide(1200, 1600, frame_portrait=False, thr=0.10)
    assert d.content_portrait is False   # upright for the frame, NOT laid sideways
    assert d.use_cover is False          # bands, not a hard crop


def test_mismatched_photo_fills_only_with_an_extreme_threshold():
    # the same ~78% gap fills once the slider allows that much zoom — the slider is the
    # ONLY fill control (this is what the removed auto-rotate toggle did unconditionally)
    d = _decide(1200, 1600, frame_portrait=False, thr=0.80)
    assert d.content_portrait is False
    assert d.use_cover is True


# ── a manual crop is NOT a special case: the same threshold governs it ───────────
# (callers pass the ALREADY-cropped dims, so a crop only changes the shape measured)

@pytest.mark.parametrize("frame_portrait", [True, False])
def test_panel_aspect_crop_fills_its_matching_frame(frame_portrait):
    """An editor crop is cut to the panel aspect, so on a frame of that shape it needs
    ~0% zoom and fills — no bars, nothing trimmed."""
    src = (1200, 1600) if frame_portrait else (1600, 1200)
    d = _decide(*src, frame_portrait=frame_portrait, thr=0.10)
    assert d.content_portrait == frame_portrait
    assert d.use_cover is True


def test_crop_on_a_mismatched_frame_is_governed_by_the_threshold_too():
    """A portrait crop on a landscape frame gets no special treatment: at a sane
    threshold it letterboxes whole rather than having a second crop cut out of it."""
    low = _decide(1200, 1600, frame_portrait=False, thr=0.10)
    high = _decide(1200, 1600, frame_portrait=False, thr=0.80)
    assert low.use_cover is False    # preserved whole, matted
    assert high.use_cover is True    # user explicitly asked for fill-at-any-cost


# ── the single slider governs every automatic fill decision ─────────────────────

def test_threshold_zero_letterboxes_mismatched_aspect():
    d = _decide(1400, 900, frame_portrait=False, thr=0.0)
    assert d.use_cover is False  # 0% threshold → never auto-fill


def test_high_threshold_fills():
    d = _decide(1400, 900, frame_portrait=False, thr=1.0)
    assert d.use_cover is True   # generous threshold → fill


def test_threshold_is_the_only_knob_boundary():
    # find a case with a known zoom ratio and verify the threshold boundary governs it.
    # landscape source 1600x900 on landscape content canvas (long=1600, short=1200):
    #   fit = min(1600/1600, 1200/900)=1.0 ; cover = max(...)=1.333 ; ratio≈0.333
    src_w, src_h = 1600, 900
    below = _decide(src_w, src_h, frame_portrait=False, thr=0.30)
    above = _decide(src_w, src_h, frame_portrait=False, thr=0.40)
    assert below.use_cover is False  # 0.333 > 0.30 → letterbox
    assert above.use_cover is True   # 0.333 <= 0.40 → fill


# ── sanity: return type ─────────────────────────────────────────────────────────

def test_returns_framing_decision():
    d = _decide(1000, 1000, frame_portrait=True)
    assert isinstance(d, FramingDecision)
