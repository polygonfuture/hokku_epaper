"""ScreenImageConfig — the complete spec for rendering one image onto the panel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from hokku_server.bounding_box import BoundingBox
from hokku_server.image_config import ImageConfig, _image_config_from_dict
from hokku_server.orientation import Orientation


#: Version of the framing policy baked into every cached render. Bumping it changes
#: every ``cache_slug()`` and therefore re-renders the library once, on the next sync.
_FRAMING_POLICY_VERSION = 2


@dataclass(frozen=True)
class ScreenImageConfig:
    """The complete spec for rendering one image onto the panel:
    which dithering pipeline, in which orientation, how aggressively to crop
    out letterbox bands, and where faces are (if detected).

    All fields together uniquely determine the panel binary output, so this
    struct is used as the cache key for panel .bin files.
    """

    image_config: ImageConfig
    orientation: Orientation
    crop_to_fill_threshold: float = 0.0
    #: Face bounding boxes or None.
    #: Passed to the renderer to scope CLAHE away from the face regions.
    clahe_keepout_bboxes: tuple[BoundingBox, ...] | None = None
    #: Manual crop/rotation from the per-image editor. Defaults (0 / None) mean
    #: "auto fit/cover" — identical behaviour for every non-edited image.
    #: rotation_quarters: 0-3 clockwise 90-degree steps, applied BEFORE cropping.
    #: crop_rect: normalized (x, y, w, h) within the rotated source frame.
    rotation_quarters: int = 0
    crop_rect: tuple[float, float, float, float] | None = None
    #: Faces the cover-crop window is aimed at (face-aware cropping). Part of the cache
    #: key: the same photo framed at the centre vs at a face is a different render.
    crop_anchor_bboxes: tuple[BoundingBox, ...] | None = None

    def cache_slug(self) -> str:
        # Convert BoundingBox objects to dicts for JSON serialization
        bbox_serializable = None
        if self.clahe_keepout_bboxes:
            bbox_serializable = [asdict(b) for b in self.clahe_keepout_bboxes]

        payload = {
            # Framing-policy version. The slug is the render cache key, so a change to
            # HOW a photo is framed (not just to these values) must move it or the old
            # panel/preview files are reused forever. Bump on every framing change.
            #   1 = original / auto-rotate era
            #   2 = auto-rotate removed: content always composed upright for the frame,
            #       a crop forces cover, otherwise crop_to_fill_threshold decides
            "framing_policy": _FRAMING_POLICY_VERSION,
            "image_config": self.image_config.cache_slug(),
            "orientation": self.orientation,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
            "clahe_keepout_bboxes": bbox_serializable,
        }
        # Only fold in manual crop/rotation when actually set, so an edited image's slug
        # differs from the same image un-edited (the version token above governs
        # cross-policy invalidation).
        if self.rotation_quarters:
            payload["rotation_quarters"] = self.rotation_quarters
        if self.crop_rect:
            payload["crop_rect"] = list(self.crop_rect)
        if self.crop_anchor_bboxes:
            payload["crop_anchor_bboxes"] = [asdict(b) for b in self.crop_anchor_bboxes]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:14]


def _screen_image_config_from_dict(d: dict) -> ScreenImageConfig:
    """Round-trip helper: dict → ScreenImageConfig."""
    image_config = _image_config_from_dict(d.get("image_config"), field_path="image_config")
    orientation = Orientation(d["orientation"])
    crop_to_fill_threshold = float(d.get("crop_to_fill_threshold", 0.0))
    raw = d.get("clahe_keepout_bboxes")
    if raw is not None:
        try:
            keepout = tuple(BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in raw)
        except (ValueError, KeyError, TypeError):
            keepout = None
    else:
        keepout = None
    rotation_quarters = int(d.get("rotation_quarters", 0))
    crop_raw = d.get("crop_rect")
    crop_rect = tuple(float(v) for v in crop_raw) if crop_raw else None
    anchor_raw = d.get("crop_anchor_bboxes")
    if anchor_raw is not None:
        try:
            anchor = tuple(
                BoundingBox(x=b["x"], y=b["y"], w=b["w"], h=b["h"]) for b in anchor_raw
            )
        except (ValueError, KeyError, TypeError):
            anchor = None
    else:
        anchor = None
    return ScreenImageConfig(
        image_config=image_config,
        orientation=orientation,
        crop_to_fill_threshold=crop_to_fill_threshold,
        clahe_keepout_bboxes=keepout,
        rotation_quarters=rotation_quarters,
        crop_rect=crop_rect,
        crop_anchor_bboxes=anchor,
    )
