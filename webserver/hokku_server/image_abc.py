"""Abstract base class for Spectra 6 image renderers.

All renderers share the same public surface:

  render_panel_bytes(img, cfg, orientation, crop_to_fill_threshold) → bytes
  render_preview_png(img, cfg, orientation, max_side_px, crop_to_fill_threshold) → bytes

The shared fit-crop → PIL enhancements → rotate pipeline lives in the
protected ``_prepare_canvas()`` template method so concrete subclasses
only need to implement the dither dispatch in ``render_indices()``.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from hokku_server.bounding_box import BoundingBox
from hokku_server.display import (
    FULL_W,
    PANEL_H,
    indices_to_panel_bytes,
    indices_to_preview_rgb,
    panel_bytes_to_indices,
)
from hokku_server.image_config import ImageConfig
from hokku_server.orientation import Orientation

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class FramingDecision:
    """The shared framing decision for one photo on one frame.

    ``content_portrait``: compose the content on a PORTRAIT working canvas (rotated into
    the physical buffer) vs a LANDSCAPE one.
    ``use_cover``: cover-crop to FILL the canvas (True) vs letterbox with white bars (False).

    Both the colour pipeline (``_prepare_canvas``) and the mono pipeline
    (``_fit_gray_to_panel``) consume this so they frame identically; each keeps its own
    canvas sizing and rotation, which are genuinely different coordinate work.
    """

    content_portrait: bool
    use_cover: bool


def frame_decision(
    src_w: int,
    src_h: int,
    canvas_w: int,
    canvas_h: int,
    *,
    frame_portrait: bool,
    auto_rotate: bool,
    has_crop: bool,
    crop_to_fill_threshold: float,
) -> FramingDecision:
    """Decide content orientation + fill-vs-letterbox for an ALREADY cropped/rotated image.

    Callers apply any manual crop/rotation to the image FIRST, then pass the transformed
    ``src_w/src_h`` here — so a manual crop is not a special case, it simply changes the
    shape being measured (a portrait crop is a portrait source). ``has_crop`` is True only
    when a real (non-full-frame) crop rect survived normalisation; it forces cover per D3.

    ``canvas_w/canvas_h`` are the PHYSICAL panel dims in either order (e.g. colour
    1200x1600, mono 1872x1404). The content canvas takes the content orientation; the
    zoom-to-fill ratio is measured against that canvas so a single ``crop_to_fill_threshold``
    governs every fill decision on both pipelines (D2).

    Rules (UNIFY_PLAN.md D1-D4):
      D1 content orientation: ON follows the FRAME, OFF follows the (cropped) SOURCE.
      D4 with a crop + auto_rotate ON, auto_rotate STILL wins the orientation (B1): the
         crop's pixels are reframed to the mount — there is no crop→auto_rotate suppression.
      D2/D3 use_cover: a real crop OR auto_rotate force cover unconditionally; otherwise
         cover only when the single adjustable ``crop_to_fill_threshold`` allows, measured
         by one normalised zoom-to-fill ratio (no fixed MAX_COVER_CROP_FRAC).
    """
    src_portrait = src_h > src_w
    content_portrait = frame_portrait if auto_rotate else src_portrait

    if has_crop or auto_rotate:
        return FramingDecision(content_portrait=content_portrait, use_cover=True)

    # Same-source-shape path (no crop, no auto_rotate). Size the content canvas to the
    # content orientation, then fill only if the zoom needed to cover it is within the
    # user's threshold — the single knob for every letterbox-vs-fill decision.
    long_side, short_side = max(canvas_w, canvas_h), min(canvas_w, canvas_h)
    visible_w, visible_h = (
        (short_side, long_side) if content_portrait else (long_side, short_side)
    )
    scale_fit = min(visible_w / src_w, visible_h / src_h)
    scale_cover = max(visible_w / src_w, visible_h / src_h)
    zoom_ratio = scale_cover / scale_fit - 1.0
    use_cover = crop_to_fill_threshold > 0.0 and zoom_ratio <= crop_to_fill_threshold
    return FramingDecision(content_portrait=content_portrait, use_cover=use_cover)


def transform_bboxes_to_canvas_norm(
    bboxes_norm: tuple[BoundingBox, ...] | None,
    orig_w: int,
    orig_h: int,
    orientation: Orientation,
    canvas_w: int,
    canvas_h: int,
    crop_to_fill_threshold: float = 0.0,
    *,
    has_crop: bool = False,
    auto_rotate: bool = False,
) -> list[tuple[float, float, float, float]]:
    """Convert face bboxes from original-image normalised coords to coords
    normalised against the rendered **preview PNG** (the PNG returned by
    ``render_preview_png``, which is already in visible orientation —
    ``_encode_panel_rgb_to_png`` rotates landscape back +90° to undo
    ``_prepare_canvas``'s -90°).

    Uses the SAME ``frame_decision`` as the render so the fit/cover scaling — and the
    content orientation of the visible canvas — match ``_prepare_canvas`` exactly. Passing
    ``orig_w/orig_h`` already reflecting any crop (the caller pre-transforms the crop), with
    ``has_crop``/``auto_rotate`` forwarded so a covered photo's boxes are not mapped as if
    letterboxed. Result is normalised against the *visible* dimensions.
    """
    if not bboxes_norm:
        return []

    decision = frame_decision(
        orig_w, orig_h, canvas_w, canvas_h,
        frame_portrait=orientation == "portrait",
        auto_rotate=auto_rotate,
        has_crop=has_crop,
        crop_to_fill_threshold=crop_to_fill_threshold,
    )
    long_side, short_side = max(canvas_w, canvas_h), min(canvas_w, canvas_h)
    visible_w, visible_h = (
        (short_side, long_side) if decision.content_portrait else (long_side, short_side)
    )

    scale_fit = min(visible_w / orig_w, visible_h / orig_h)
    scale_cover = max(visible_w / orig_w, visible_h / orig_h)
    use_cover = decision.use_cover

    if use_cover:
        scaled_w = max(visible_w, math.ceil(orig_w * scale_cover))
        scaled_h = max(visible_h, math.ceil(orig_h * scale_cover))
        x_off = (scaled_w - visible_w) // 2
        y_off = (scaled_h - visible_h) // 2
    else:
        scaled_w = orig_w * scale_fit
        scaled_h = orig_h * scale_fit
        x_off = (visible_w - scaled_w) / 2
        y_off = (visible_h - scaled_h) / 2

    out: list[tuple[float, float, float, float]] = []
    for bbox in bboxes_norm:
        if use_cover:
            fx = bbox.x * scaled_w - x_off
            fy = bbox.y * scaled_h - y_off
        else:
            fx = bbox.x * scaled_w + x_off
            fy = bbox.y * scaled_h + y_off
        fw = bbox.w * scaled_w
        fh = bbox.h * scaled_h

        # Clamp to visible bounds
        fx = max(0.0, fx)
        fy = max(0.0, fy)
        fw = max(0.0, min(fw, visible_w - fx))
        fh = max(0.0, min(fh, visible_h - fy))

        out.append((fx / visible_w, fy / visible_h, fw / visible_w, fh / visible_h))
    return out


def preview_png_from_panel_bytes(panel_bytes: bytes, orientation: Orientation) -> bytes:
    """Decode an already-rendered panel binary back to a PNG preview."""
    idx = panel_bytes_to_indices(panel_bytes)
    rgb = indices_to_preview_rgb(idx)
    return AbstractImageRenderer._encode_panel_rgb_to_png(rgb, orientation)


def _apply_prepare_enhancements(
    canvas: Image.Image,
    cfg: ImageConfig,
    keepout_bboxes_canvas: list[tuple[int, int, int, int]] | None = None,
) -> Image.Image:
    # 1. Global autocontrast
    canvas = ImageOps.autocontrast(canvas, cutoff=cfg.prepare_autocontrast_cutoff)

    # 2. Gamma correction (power curve via uint8 LUT)
    gamma_lut = [int(((i / 255.0) ** cfg.prepare_gamma) * 255) for i in range(256)] * 3
    canvas = canvas.point(gamma_lut)

    # 3. Midtone lift (separate power curve; 1.0 = identity, skipped for speed)
    if cfg.prepare_midtone != 1.0:
        exp = 1.0 / cfg.prepare_midtone
        midtone_lut = [round(255 * (i / 255.0) ** exp) if i > 0 else 0 for i in range(256)] * 3
        canvas = canvas.point(midtone_lut)

    # 4. Brightness / contrast
    canvas = ImageEnhance.Brightness(canvas).enhance(cfg.prepare_brightness)
    canvas = ImageEnhance.Contrast(canvas).enhance(cfg.prepare_contrast)

    # 5. CLAHE local contrast on Lab L* channel (skipped when clip_limit == 0).
    #    When face bboxes are provided, the face region's L* is restored after
    #    CLAHE so the local contrast expansion doesn't blow out skin highlights.
    if cfg.clahe_clip_limit > 0.0:
        arr = np.asarray(canvas, dtype=np.uint8)
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        clahe = cv2.createCLAHE(
            clipLimit=cfg.clahe_clip_limit,
            tileGridSize=(8, 8),
        )
        if keepout_bboxes_canvas:
            original_L = lab[:, :, 0].copy()
            clahe_L = clahe.apply(lab[:, :, 0])
            feather = cfg.clahe_keepout_feather
            if feather > 0.0:
                canvas_w, canvas_h = canvas.size
                sigma = min(canvas_w, canvas_h) * feather
                mask = np.zeros(lab.shape[:2], dtype=np.float32)
                for fx, fy, fw, fh in keepout_bboxes_canvas:
                    if fw > 0 and fh > 0:
                        mask[fy : fy + fh, fx : fx + fw] = 1.0
                mask = cv2.GaussianBlur(mask, (0, 0), sigma)
                blended = original_L.astype(np.float32) * mask + clahe_L.astype(np.float32) * (
                    1.0 - mask
                )
                lab[:, :, 0] = np.clip(blended, 0, 255).astype(np.uint8)
            else:
                lab[:, :, 0] = clahe_L
                for fx, fy, fw, fh in keepout_bboxes_canvas:
                    lab[fy : fy + fh, fx : fx + fw, 0] = original_L[fy : fy + fh, fx : fx + fw]
        else:
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        canvas = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))

    # 6. Unsharp mask sharpening (replaces fixed PIL Sharpness kernel)
    canvas = canvas.filter(
        ImageFilter.UnsharpMask(
            radius=cfg.prepare_usm_radius,
            percent=cfg.prepare_usm_amount,
            threshold=3,  # ignore diffs < 3 to avoid sharpening noise
        )
    )

    # 7. Colour enhance (only when adaptive_saturate is off — both boost chroma)
    if cfg.adaptive_saturate_space == "off":
        canvas = ImageEnhance.Color(canvas).enhance(cfg.color_enhance)

    return canvas


def _rotate_norm_bbox_cw(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    """One clockwise 90-degree turn of a normalized (x, y, w, h) box."""
    return (1.0 - y - h, x, h, w)


def effective_cropped_dims(
    src_w: int,
    src_h: int,
    rotation_quarters: int,
    crop_rect: tuple[float, float, float, float] | None,
) -> tuple[int, int]:
    """The (w, h) an image would have after ``_apply_crop_rotation`` — WITHOUT rendering.

    Single source of truth for "what shape is this after the manual edit?", so the render
    path (which applies the crop then measures) and the preview un-rotate path (which must
    predict the same orientation) never disagree. Mirrors _apply_crop_rotation's geometry.
    """
    w, h = src_w, src_h
    if rotation_quarters % 4:
        # a 90/270 rotate swaps the dimensions
        if rotation_quarters % 2:
            w, h = h, w
    if crop_rect:
        _x, _y, cw, ch = crop_rect
        # crop rect is normalised in the rotated frame; the same clamping as
        # _apply_crop_rotation (round, min width/height of 1px)
        left = max(0, min(w - 1, round(_x * w)))
        top = max(0, min(h - 1, round(_y * h)))
        right = max(left + 1, min(w, round((_x + cw) * w)))
        bottom = max(top + 1, min(h, round((_y + ch) * h)))
        w, h = right - left, bottom - top
    return w, h


def _apply_crop_rotation(
    img: Image.Image,
    rotation_quarters: int,
    crop_rect: tuple[float, float, float, float] | None,
) -> Image.Image:
    """Rotate the source clockwise by ``rotation_quarters * 90`` then crop to the
    normalized rect (expressed in the rotated frame). Returns a NEW image; the
    caller keeps ownership of the input ``img``.
    """
    out = img
    q = rotation_quarters % 4
    if q:
        out = out.rotate(-90 * q, expand=True)  # PIL rotate is CCW, so -90 == one CW quarter
    if crop_rect:
        rw, rh = out.size
        x, y, w, h = crop_rect
        left = max(0, min(rw - 1, round(x * rw)))
        top = max(0, min(rh - 1, round(y * rh)))
        right = max(left + 1, min(rw, round((x + w) * rw)))
        bottom = max(top + 1, min(rh, round((y + h) * rh)))
        cropped = out.crop((left, top, right, bottom))
        if out is not img:
            out.close()
        out = cropped
    elif out is img:
        out = img.copy()  # always hand back a distinct object the caller can release
    return out


def _transform_keepout_through_crop(
    bboxes_norm: tuple[BoundingBox, ...],
    rotation_quarters: int,
    crop_rect: tuple[float, float, float, float] | None,
) -> tuple[BoundingBox, ...]:
    """Re-express original-image-normalized keepout bboxes in the rotated+cropped
    frame so CLAHE keepout stays on the right pixels for edited images.
    """
    q = rotation_quarters % 4
    cx, cy, cw, ch = crop_rect if crop_rect else (0.0, 0.0, 1.0, 1.0)
    out: list[BoundingBox] = []
    for b in bboxes_norm:
        x, y, w, h = b.x, b.y, b.w, b.h
        for _ in range(q):
            x, y, w, h = _rotate_norm_bbox_cw(x, y, w, h)
        x, y, w, h = (x - cx) / cw, (y - cy) / ch, w / cw, h / ch
        nx, ny = max(0.0, x), max(0.0, y)
        nx2, ny2 = min(1.0, x + w), min(1.0, y + h)
        if nx2 > nx and ny2 > ny:
            out.append(BoundingBox(x=nx, y=ny, w=nx2 - nx, h=ny2 - ny))
    return tuple(out)


class AbstractImageRenderer(ABC):
    """Strategy interface for panel-image rendering.

    ``_prepare_canvas()`` is the shared template: it handles fit/crop,
    PIL enhancements, and rotation, returning a uint8 numpy array and a
    boolean padding mask.  Concrete subclasses implement ``render_indices()``
    which calls ``_prepare_canvas()`` and dispatches to their dither strategy.
    """

    # ── Static helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _preview_canvas_dims(orientation: Orientation, max_side_px: int) -> tuple[int, int]:
        """Scale (FULL_W, PANEL_H) so the longer side is ≤ max_side_px."""
        s = min(1.0, float(max_side_px) / float(max(FULL_W, PANEL_H)))
        cw = max(1, int(FULL_W * s))
        ch = max(1, int(PANEL_H * s))
        return cw, ch

    @staticmethod
    def _encode_panel_rgb_to_png(panel_rgb: NDArray[np.uint8], orientation: Orientation) -> bytes:
        """Panel-memory RGB → PNG bytes in the visible (browser) orientation."""
        img = Image.fromarray(np.asarray(panel_rgb, dtype=np.uint8))
        if orientation == "landscape":
            img = img.rotate(90, expand=True)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ── Template method ────────────────────────────────────────────────────

    def _prepare_canvas(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        canvas_w: int,
        canvas_h: int,
        crop_to_fill_threshold: float = 0.0,
        *,
        release_input: bool = False,
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
        rotation_quarters: int = 0,
        crop_rect: tuple[float, float, float, float] | None = None,
        auto_rotate: bool = False,
    ) -> tuple[NDArray[np.uint8], NDArray[np.bool_]]:
        """Fit-or-crop → PIL enhancements → rotate.

        Returns ``(uint8_array, padding_mask)`` where ``padding_mask`` is
        True for pixels that are white letterbox padding and should be forced
        to palette index 1 (white ink) after dithering.

        clahe_keepout_bboxes_norm: [(x, y, w, h), ...] in [0, 1] relative to the original image.
        Converted to canvas pixel coordinates and passed to
        _apply_prepare_enhancements to scope CLAHE away from the face regions.

        rotation_quarters / crop_rect: optional manual edit from the per-image
        editor. Rotate the source clockwise then crop to the normalized rect (in
        the rotated frame) BEFORE the usual fit/cover scaling. Defaults (0 / None)
        are the unchanged auto path.
        """
        if rotation_quarters or crop_rect:
            transformed = _apply_crop_rotation(img, rotation_quarters, crop_rect)
            if release_input:
                img.close()
            img = transformed
            release_input = True  # `img` is now our own copy; downstream may release it
            if clahe_keepout_bboxes_norm:
                clahe_keepout_bboxes_norm = (
                    _transform_keepout_through_crop(
                        clahe_keepout_bboxes_norm, rotation_quarters, crop_rect
                    )
                    or None
                )

        # ── Shared framing decision (frame_decision, consumed by mono too) ──
        # `img` has ALREADY been cropped/rotated above, so its size reflects the crop's
        # target shape — a portrait crop is a portrait source (D1). The decision picks the
        # CONTENT orientation and whether to cover-fill; the canvas sizing + rotation below
        # are this pipeline's own coordinate work and stay local.
        frame_portrait = orientation == "portrait"
        src_w, src_h = img.size
        decision = frame_decision(
            src_w, src_h, canvas_w, canvas_h,
            frame_portrait=frame_portrait,
            auto_rotate=auto_rotate,
            has_crop=crop_rect is not None,
            crop_to_fill_threshold=crop_to_fill_threshold,
        )
        content_portrait = decision.content_portrait
        use_cover = decision.use_cover

        # Working canvas sized to the CONTENT orientation (either arg order accepted). The
        # final -90 rotate below maps a landscape working canvas into the portrait panel
        # buffer; a content canvas whose orientation differs from the frame is what makes an
        # OFF orientation-MISMATCH fill instead of letterboxing.
        long_side, short_side = max(canvas_w, canvas_h), min(canvas_w, canvas_h)
        visible_w, visible_h = (
            (short_side, long_side) if content_portrait else (long_side, short_side)
        )
        needs_final_rotate = (visible_w, visible_h) != (canvas_w, canvas_h)

        scale_fit = min(visible_w / src_w, visible_h / src_h)
        scale_cover = max(visible_w / src_w, visible_h / src_h)

        keepout_canvas: list[tuple[int, int, int, int]] = []

        if use_cover:
            scaled_w = max(visible_w, math.ceil(src_w * scale_cover))
            scaled_h = max(visible_h, math.ceil(src_h * scale_cover))
            img_scaled = img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
            if release_input:
                img.close()
            x_off = (scaled_w - visible_w) // 2
            y_off = (scaled_h - visible_h) // 2
            composed = img_scaled.crop((x_off, y_off, x_off + visible_w, y_off + visible_h))
            img_scaled.close()
            padding_mask = np.zeros((visible_h, visible_w), dtype=bool)
            # Bbox in canvas coords for cover: scale by cover scale, subtract crop offset
            if clahe_keepout_bboxes_norm:
                for bbox in clahe_keepout_bboxes_norm:
                    fx = int(bbox.x * scaled_w) - x_off
                    fy = int(bbox.y * scaled_h) - y_off
                    fw = int(bbox.w * scaled_w)
                    fh = int(bbox.h * scaled_h)
                    keepout_canvas.append(
                        (
                            max(0, fx),
                            max(0, fy),
                            min(fw, visible_w - max(0, fx)),
                            min(fh, visible_h - max(0, fy)),
                        )
                    )
        else:
            scale = scale_fit
            new_w, new_h = int(src_w * scale), int(src_h * scale)
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            if release_input:
                img.close()
            composed = Image.new("RGB", (visible_w, visible_h), (255, 255, 255))
            x_off = (visible_w - new_w) // 2
            y_off = (visible_h - new_h) // 2
            composed.paste(img_resized, (x_off, y_off))
            img_resized.close()
            padding_mask = np.ones((visible_h, visible_w), dtype=bool)
            padding_mask[y_off : y_off + new_h, x_off : x_off + new_w] = False
            # Bbox in canvas coords for fit: scale by fit scale, add letterbox offset
            if clahe_keepout_bboxes_norm:
                for bbox in clahe_keepout_bboxes_norm:
                    fx = int(bbox.x * new_w) + x_off
                    fy = int(bbox.y * new_h) + y_off
                    fw = int(bbox.w * new_w)
                    fh = int(bbox.h * new_h)
                    keepout_canvas.append(
                        (
                            max(0, fx),
                            max(0, fy),
                            min(fw, visible_w - max(0, fx)),
                            min(fh, visible_h - max(0, fy)),
                        )
                    )

        composed = _apply_prepare_enhancements(composed, cfg, keepout_canvas or None)

        # Rotate the working canvas into the physical panel buffer when it is not already
        # in the buffer's (canvas_h, canvas_w) arrangement (see needs_final_rotate above).
        # Gated on shapes, not the frame — so an OFF orientation-mismatch (e.g. portrait
        # photo, landscape frame) keeps its content-shaped canvas, fills, and the user
        # turns the frame (mirrors the mono pipeline).
        if needs_final_rotate:
            composed = composed.rotate(-90, expand=True)
            padding_mask = np.rot90(padding_mask, k=3)

        arr = np.asarray(composed, dtype=np.uint8)
        composed = None
        return arr, padding_mask

    # ── Abstract core ──────────────────────────────────────────────────────

    @abstractmethod
    def render_indices(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        canvas_w: int,
        canvas_h: int,
        crop_to_fill_threshold: float = 0.0,
        *,
        release_input: bool = False,
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
        rotation_quarters: int = 0,
        crop_rect: tuple[float, float, float, float] | None = None,
        auto_rotate: bool = False,
    ) -> NDArray[np.uint8]:
        """Render to palette indices.

        Implementations call ``self._prepare_canvas(...)`` and then apply
        their dither strategy.  Returns H×W uint8 palette-index array.
        """

    # ── Concrete public API ────────────────────────────────────────────────

    def render_panel_bytes(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        crop_to_fill_threshold: float = 0.0,
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
        *,
        rotation_quarters: int = 0,
        crop_rect: tuple[float, float, float, float] | None = None,
        auto_rotate: bool = False,
    ) -> bytes:
        """Full-resolution panel → wire bytes."""
        idx = self.render_indices(
            img,
            cfg,
            orientation,
            FULL_W,
            PANEL_H,
            crop_to_fill_threshold,
            release_input=True,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
            rotation_quarters=rotation_quarters,
            crop_rect=crop_rect,
            auto_rotate=auto_rotate,
        )
        return indices_to_panel_bytes(idx)

    def render_preview_png(
        self,
        img: Image.Image,
        cfg: ImageConfig,
        orientation: Orientation,
        max_side_px: int = 800,
        crop_to_fill_threshold: float = 0.0,
        clahe_keepout_bboxes_norm: tuple[BoundingBox, ...] | None = None,
        *,
        rotation_quarters: int = 0,
        crop_rect: tuple[float, float, float, float] | None = None,
        auto_rotate: bool = False,
    ) -> bytes:
        """Smaller panel → PNG preview bytes. ``auto_rotate`` mirrors the serve render so
        the preview matches the glass when the global fit-to-frame toggle is on."""
        cw, ch = self._preview_canvas_dims(orientation, max_side_px)
        idx = self.render_indices(
            img,
            cfg,
            orientation,
            cw,
            ch,
            crop_to_fill_threshold,
            clahe_keepout_bboxes_norm=clahe_keepout_bboxes_norm,
            rotation_quarters=rotation_quarters,
            crop_rect=crop_rect,
            auto_rotate=auto_rotate,
        )
        preview_rgb = indices_to_preview_rgb(idx)
        return self._encode_panel_rgb_to_png(preview_rgb, orientation)
