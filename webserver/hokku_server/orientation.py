"""Orientation enum shared across the image pipeline."""

from __future__ import annotations

import enum


class Orientation(str, enum.Enum):
    """Display orientation of the e-ink panel.

    ``str`` base class keeps ``Orientation.LANDSCAPE == "landscape"`` true and
    JSON serialisation producing plain strings, so existing comparisons and
    config files are unaffected.
    """

    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
    NEUTRAL = "neutral"  # square/unknown image — eligible for any screen orientation


def orientation_from_dims(width: int, height: int) -> Orientation:
    """Concrete render orientation from pixel dimensions, with NO OK-status assert.

    Unlike ``ImageRecord.native_orientation`` this works on drafts and images that
    are not yet converted. A square image falls to landscape; callers that know the
    real target (e.g. the editor) should pass an explicit orientation instead.
    """
    return Orientation.PORTRAIT if height > width else Orientation.LANDSCAPE
