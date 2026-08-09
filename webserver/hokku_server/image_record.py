"""ImageRecord dataclass and serialisation helpers."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field

from hokku_server.orientation import Orientation


class ConvertStatus(str, enum.Enum):
    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"
    DRAFT = "draft"  # uploaded for editing but inert — never auto-converts or joins rotation


@dataclass(frozen=True)
class ImageRecord:
    name: str  # outside-world identifier
    name_hash: str  # sha1(name) — on-disk identifier
    original_sha1: str  # sha1 of file contents
    original_size_bytes: int
    original_mtime: float
    added_at: float
    convert_status: ConvertStatus
    convert_error: str | None
    landscape_image_config_slug: str | None = None  # ScreenImageConfig slug for landscape render
    portrait_image_config_slug: str | None = None  # ScreenImageConfig slug for portrait render
    last_conversion_seconds: float | None = None  # wall-clock time of last successful render
    image_width: int | None = None  # pixel dimensions of the source image
    image_height: int | None = None
    #: Per-image editor overrides (None = use the classifier's decision / auto crop).
    #: edit_image_config: a frozen ImageConfig blob. edit_crop: {rotation_quarters,
    #: rect{x,y,w,h}, target}. edit_mono: a per-image E1003 tone override — the flat
    #: short-name mono knob set ({black_point, clarity, contrast, ...}); each missing
    #: knob inherits the panel-wide mono_e1003_* default at serve time (None = fully
    #: inherit). edit_crop_mono: a per-image E1003 crop OVERRIDE, same shape as edit_crop
    #: ({rotation_quarters, rect{x,y,w,h}, target}). None = the mono panel FOLLOWS the
    #: colour edit_crop (inherit-by-default); a value forks an independent mono framing.
    #: Excluded from __hash__ (dicts are unhashable) but kept in equality so
    #: round-trips and change-detection still see them. edit_mono/edit_crop_mono are
    #: tolerated on load (``.get`` below) so no DB version bump / re-render is needed.
    edit_image_config: dict | None = field(default=None, hash=False)
    edit_crop: dict | None = field(default=None, hash=False)
    edit_mono: dict | None = field(default=None, hash=False)
    edit_crop_mono: dict | None = field(default=None, hash=False)

    def slug(self, orientation: Orientation) -> str | None:
        """Return the cached slug for the given orientation, or None if not yet rendered."""
        assert orientation != Orientation.NEUTRAL, "slug() requires LANDSCAPE or PORTRAIT"
        return (
            self.landscape_image_config_slug
            if orientation == Orientation.LANDSCAPE
            else self.portrait_image_config_slug
        )

    @property
    def native_orientation(self) -> Orientation:
        """Returns LANDSCAPE, PORTRAIT, or NEUTRAL (square).

        Only valid for ok-status images — callers must ensure convert_status == ConvertStatus.OK.
        """
        assert self.convert_status == ConvertStatus.OK, (
            f"native_orientation called on non-ok image {self.name!r} "
            f"(status={self.convert_status})"
        )
        w, h = self.image_width, self.image_height
        assert w and h, f"ok-status image {self.name!r} has invalid dimensions ({w}, {h})"
        if w == h:
            return Orientation.NEUTRAL
        return Orientation.LANDSCAPE if w > h else Orientation.PORTRAIT

    @property
    def effective_orientation(self) -> Orientation:
        """The orientation this image should render/serve as: the editor's chosen
        target when it carries a crop, otherwise the source's native orientation."""
        if self.edit_crop:
            target = self.edit_crop.get("target")
            if target:
                return Orientation(target)
        return self.native_orientation

    def crop_for(self, orientation: Orientation) -> dict | None:
        """The manual crop to render this image with on a frame of ``orientation``, or
        None to render it as if it had never been cropped.

        An editor crop is authored FOR a shape — the editor records that shape in the
        crop's ``target``. On a frame of the OTHER shape the crop does not apply: reusing
        it there would show a tall crop in a wide frame (bars, or a second crop cut out of
        the user's framing), while the untouched original usually suits that frame far
        better. So the photo simply behaves as if unedited there — same pixels, same
        slider, same face-aware framing as any uncropped photo. One crop per photo covers
        its own shape; nothing has to be authored for the other.

        A legacy crop with no ``target`` applies everywhere (its pre-target behaviour).
        """
        crop = self.edit_crop
        if not crop:
            return None
        target = crop.get("target")
        if not target:
            return crop
        return crop if Orientation(target) == orientation else None

    def matches_orientation_filter(self, orientation: Orientation) -> bool:
        """True if this image is eligible when a screen filters by the given orientation.

        Uses ``effective_orientation`` so a manual crop redefines eligibility: a landscape
        source cropped to portrait is portrait-eligible (and no longer landscape-eligible),
        matching how it renders and what the detail view reports. A square source with no
        crop still matches either filter (effective_orientation == NEUTRAL)."""
        assert orientation != Orientation.NEUTRAL, "pass LANDSCAPE or PORTRAIT to a filter"
        return self.effective_orientation in (Orientation.NEUTRAL, orientation)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ImageRecord:
        raw_t = d.get("last_conversion_seconds")
        raw_w, raw_h = d.get("image_width"), d.get("image_height")
        return cls(
            name=d["name"],
            name_hash=d["name_hash"],
            original_sha1=d["original_sha1"],
            original_size_bytes=int(d["original_size_bytes"]),
            original_mtime=float(d["original_mtime"]),
            added_at=float(d["added_at"]),
            convert_status=ConvertStatus(d["convert_status"]),
            convert_error=d.get("convert_error"),
            landscape_image_config_slug=d.get("landscape_image_config_slug"),
            portrait_image_config_slug=d.get("portrait_image_config_slug"),
            last_conversion_seconds=float(raw_t) if raw_t is not None else None,
            image_width=int(raw_w) if raw_w is not None else None,
            image_height=int(raw_h) if raw_h is not None else None,
            edit_image_config=d.get("edit_image_config"),
            edit_crop=d.get("edit_crop"),
            edit_mono=d.get("edit_mono"),
            edit_crop_mono=d.get("edit_crop_mono"),
        )


@dataclass(frozen=True)
class ConversionProgress:
    current_name: str | None  # being converted right now (None if idle)
    done: int  # completed this sync cycle
    total: int  # scheduled this sync cycle
