"""ImageRecord.matches_orientation_filter must key on EFFECTIVE orientation.

A manual crop redefines a photo's orientation for framing: a landscape source
cropped to portrait is portrait, and must be eligible on a portrait-filtering
frame (and no longer on a landscape one). This mirrors how the photo renders and
what the detail view reports, and fixes a live serve bug where such a photo was
silently excluded from a portrait frame's rotation (only reachable via a manual
"send anyway" push).
"""

from __future__ import annotations

import time

from hokku_server.image_record import ImageRecord, Orientation


def _rec(name: str, width: int, height: int, edit_crop: dict | None = None) -> ImageRecord:
    return ImageRecord(
        name=name,
        name_hash=name[:14].ljust(14, "0"),
        original_sha1="aabbcc",
        original_size_bytes=1,
        original_mtime=time.time(),
        added_at=time.time(),
        convert_status="ok",  # type: ignore[arg-type]
        convert_error=None,
        image_width=width,
        image_height=height,
        edit_crop=edit_crop,
    )


_PORTRAIT_CROP = {
    "rotation_quarters": 0,
    "rect": {"x": 0.236, "y": 0.0, "w": 0.5627, "h": 1.0},
    "target": "portrait",
}
_LANDSCAPE_CROP = {
    "rotation_quarters": 0,
    "rect": {"x": 0.0, "y": 0.2, "w": 1.0, "h": 0.55},
    "target": "landscape",
}


def test_landscape_source_cropped_portrait_is_portrait_eligible():
    """A real case: a 1498x1124 landscape file cropped to portrait."""
    rec = _rec("landscape_crop.jpeg", 1498, 1124, edit_crop=_PORTRAIT_CROP)
    assert rec.native_orientation is Orientation.LANDSCAPE
    assert rec.effective_orientation is Orientation.PORTRAIT
    assert rec.matches_orientation_filter(Orientation.PORTRAIT) is True
    assert rec.matches_orientation_filter(Orientation.LANDSCAPE) is False


def test_portrait_source_cropped_landscape_is_landscape_eligible():
    """Symmetric case: a portrait file cropped to landscape."""
    rec = _rec("p.jpeg", 1124, 1498, edit_crop=_LANDSCAPE_CROP)
    assert rec.native_orientation is Orientation.PORTRAIT
    assert rec.effective_orientation is Orientation.LANDSCAPE
    assert rec.matches_orientation_filter(Orientation.LANDSCAPE) is True
    assert rec.matches_orientation_filter(Orientation.PORTRAIT) is False


def test_uncropped_photos_unchanged():
    """No crop → effective == native → filter behaviour is the legacy behaviour."""
    land = _rec("land.jpeg", 1498, 1124)
    assert land.matches_orientation_filter(Orientation.LANDSCAPE) is True
    assert land.matches_orientation_filter(Orientation.PORTRAIT) is False

    port = _rec("port.jpeg", 1124, 1498)
    assert port.matches_orientation_filter(Orientation.PORTRAIT) is True
    assert port.matches_orientation_filter(Orientation.LANDSCAPE) is False


def test_square_source_matches_either_filter():
    """A neutral (square) source with no crop still fits either panel."""
    sq = _rec("sq.jpeg", 1200, 1200)
    assert sq.native_orientation is Orientation.NEUTRAL
    assert sq.matches_orientation_filter(Orientation.PORTRAIT) is True
    assert sq.matches_orientation_filter(Orientation.LANDSCAPE) is True


def test_square_source_cropped_portrait_becomes_portrait_only():
    """A square source cropped to portrait is portrait — no longer matches landscape.
    Consistent with 'cropped to portrait => portrait'; a deliberate behaviour choice."""
    sq = _rec("sq2.jpeg", 1200, 1200, edit_crop=_PORTRAIT_CROP)
    assert sq.effective_orientation is Orientation.PORTRAIT
    assert sq.matches_orientation_filter(Orientation.PORTRAIT) is True
    assert sq.matches_orientation_filter(Orientation.LANDSCAPE) is False
