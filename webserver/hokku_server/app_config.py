"""AppConfig dataclass: persisted server settings.

Strict, schema-driven parser with a version field and migration chain.
Unversioned configs (no "version" key) are silently replaced with a
fresh default v1 on first load.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from hokku_server.filesystem import atomic_write_json
from hokku_server.image_config import (
    ImageConfig,
    _image_config_from_dict,
)
from hokku_server.presets import PRESET_IMAGE_CONFIGS

logger = logging.getLogger(__name__)

_CURRENT_VERSION = 6


def _migrate_v1_to_v2(d: dict) -> dict:
    """Add image_worker_thread_count (default 1 = serial, matching old behaviour)."""
    d["image_worker_thread_count"] = 1
    return d


def _migrate_v2_to_v3(d: dict) -> dict:
    """Add face_detector (default 'yunet_opencv', matching pre-3.0 behaviour)."""
    d["face_detector"] = "yunet_opencv"
    return d


def _migrate_v3_to_v4(d: dict) -> dict:
    """Add mdns_hostname (default 'hokku' — advertise as hokku.local).

    Replaces the old boolean mdns_enabled field. Empty string = off.
    """
    d.pop("mdns_enabled", None)  # remove boolean if present from earlier alpha
    d["mdns_hostname"] = "hokku"
    return d


def _migrate_v4_to_v5(d: dict) -> dict:
    """Remove face_detector — there is now only one backend (yunet_opencv)."""
    d.pop("face_detector", None)
    return d


def _migrate_v5_to_v6(d: dict) -> dict:
    """Drop the global orientation — orientation is now per-screen only."""
    d.pop("orientation", None)
    return d


# v(N) → v(N+1) upgrade functions. Populated as the schema evolves.
_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
}


def _migrate(data: dict) -> dict:
    """Walk the migration chain to the current version."""
    ver = int(data["version"])
    while ver < _CURRENT_VERSION:
        data = _MIGRATIONS[ver](data)
        ver += 1
        data["version"] = ver
    return data


@dataclass(frozen=True)
class AppConfig:
    """Persisted server settings."""

    version: int = _CURRENT_VERSION
    refresh_image_at_time: tuple[str, ...] = ("0600", "1200", "1800")
    upload_dir: str = "/var/lib/hokku/images"
    cache_dir: str = "/var/lib/hokku/cache"
    port: int = 8080
    poll_interval_seconds: int = 10
    debug_fast_refresh: bool = False
    #: Dev/debug: write a durable per-serve decision log (serve_decisions.jsonl) recording what
    #: the drawer showed vs what actually served. OFF by default (clean production); size-bounded.
    serve_decision_log_enabled: bool = False
    auto_clear_cache: bool = False
    #: Zoom up to this fraction (e.g. 0.02 = 2 %) to eliminate letterbox bands.
    #: 0.0 = always letterbox (default, safe).
    crop_to_fill_threshold: float = 0.10
    #: Auto-rotate & fit photos to each frame's native orientation (global, both panels).
    #: OFF (default): a photo keeps its own orientation — colour letterboxes an
    #: off-orientation photo upright; the mono E1003 stores a portrait photo rotated
    #: sideways in its landscape wire buffer (view by physically turning the frame).
    #: ON: crop + rotate every photo to fill the frame's orientation upright (a portrait
    #: on a landscape frame becomes an upright landscape crop, losing top/bottom).
    #: Priority per photo: manual editor crop > auto_rotate_fit > crop_to_fill_threshold.
    auto_rotate_fit: bool = False
    #: Number of worker processes for parallel image rendering.
    #: 0 = auto (cpu_count − 1, capped by available RAM at ~50 MB/worker).
    #: 1 = serial (legacy default).
    #: N > 1 = exactly N workers; the user is responsible for having enough RAM.
    image_worker_thread_count: int = 0

    # Image pipeline: default, B&W, and face presets.
    image_config_default: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["floyd_steinberg_hue_aware"]
    )
    classifier_bw_detect_enabled: bool = True
    image_config_bw: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["floyd_steinberg_bw"]
    )
    classifier_face_detect_enabled: bool = True
    classifier_face_detect_clahe_keepout: bool = True
    image_config_face: ImageConfig = field(
        default_factory=lambda: PRESET_IMAGE_CONFIGS["atkinson_hue_aware"]
    )

    #: E1003 monochrome panel — pre-dither sharpening (global default; a
    #: per-image override comes later). Kept as plain AppConfig fields, separate
    #: from the Spectra ``prepare_usm_*`` knobs: the mono and color pipelines are
    #: distinct and tuned to different values, and an image can go to both panels.
    #: Defaults are the on-glass-tuned values from mono_e1003.py. radius = halo
    #: WIDTH (keep small ~1.2), amount = STRENGTH (can run high at a small radius).
    mono_e1003_sharpen_radius: float = 1.2
    mono_e1003_sharpen_amount: int = 170
    mono_e1003_sharpen_threshold: int = 2
    #: Dither palette: False = uniform sRGB steps (eye-tuned default); True = this
    #: unit's measured level reflectances (faithful tone). darken_gamma is the
    #: midtone-darkening clamp — re-tune it when the measured ramp is on.
    #: Both are hot-reloadable so the two tunes can be A/B'd on glass.
    mono_e1003_use_measured_ramp: bool = False
    mono_e1003_darken_gamma: float = 1.40
    #: Split-screen A/B diagnostic: left half = uniform ramp (γ 1.40), right half
    #: = measured ramp + envelope (γ = mono_e1003_darken_gamma). For on-glass tone
    #: comparison; leave False for normal rendering.
    mono_e1003_split_compare: bool = False
    #: A/B toggle diagnostic: when true, each E1003 refresh ALTERNATES between
    #: Version A (uniform, γ1.40) and Version B (measured + envelope, γ =
    #: mono_e1003_darken_gamma) — so the physical refresh button swaps the two on
    #: the glass. Overrides split_compare. Leave False for normal rendering.
    mono_e1003_ab_toggle: bool = False
    #: Measured-ramp only: extra lift of the envelope's black target above the
    #: panel's physical floor (0.143). 0 = deepest tones map to level 0 (solid
    #: black, can look flat/crushed); raising it maps them to level 1-2 so the
    #: darkest shadows keep dither detail (less crushed, slightly grayer black).
    mono_e1003_shadow_lift: float = 0.0
    #: Panel-wide mono tone controls (the Custom mono editor). ALL default to a
    #: no-op so the panel's Faithful default look is unchanged until a slider moves.
    #: black/white point are bidirectional (±0.3): +black lifts the black floor
    #: (opens shadows), −black deepens; +white pushes highlights to paper white,
    #: −white pulls them down. clarity = large-radius local-contrast % (0=off; the
    #: key "wide DR feel" lever on a short-throw panel). contrast = S-curve strength
    #: (1.15 default ≈ the shipped look; 1.0 = linear). midtone = direct gamma
    #: (1.0=off, >1 darker mids). highlights/shadows = regional roll (±0.5).
    mono_e1003_black_point: float = 0.0
    mono_e1003_white_point: float = 0.0
    mono_e1003_clarity: float = 0.0
    mono_e1003_contrast: float = 1.15
    mono_e1003_midtone: float = 1.0
    mono_e1003_highlights: float = 0.0
    mono_e1003_shadows: float = 0.0

    #: E1003 tone PROFILE. "faithful" = the frozen legacy ramp pipeline above
    #: (never edited); "bw_contrast" = the fixed darktable+Lightroom preset;
    #: "custom" = the editable dtcore controls below. The dtcore path (sigmoid +
    #: local-laplacian + Lightroom-matched Basic + CLAHE) is verified 1:1 vs
    #: darktable/Lightroom. A fresh Custom starts from the B&W Contrast values.
    mono_e1003_profile: str = "faithful"
    #: darktable sigmoid (scene->display tone).
    mono_e1003_sig_enabled: bool = True
    mono_e1003_sig_contrast: float = 0.735
    mono_e1003_sig_skew: float = 1.0
    mono_e1003_sig_white: float = 100.0
    mono_e1003_sig_black: float = 0.7634
    #: darktable local-laplacian (detail=clarity %, hi/lo/midtone as in bilat.c).
    mono_e1003_lc_enabled: bool = True
    mono_e1003_lc_detail: float = 1.39
    mono_e1003_lc_highlights: float = 0.5
    mono_e1003_lc_shadows: float = 0.5
    mono_e1003_lc_midtone: float = 0.5
    #: Lightroom-matched Basic (exposure in EV; contrast/hl/sh/wh/bk in [-1,1]) +
    #: CLAHE local contrast (cv2 clipLimit 0-5; 0 = off).
    mono_e1003_basic_enabled: bool = True
    mono_e1003_basic_exposure: float = 0.0
    mono_e1003_basic_contrast: float = 0.0
    mono_e1003_basic_highlights: float = 0.0
    mono_e1003_basic_shadows: float = 0.0
    mono_e1003_basic_whites: float = 0.0
    mono_e1003_basic_blacks: float = 0.0
    mono_e1003_basic_clahe: float = 0.0

    #: mDNS / Bonjour hostname (the part before ``.local``).
    #: The server advertises itself as ``<mdns_hostname>.local`` on the LAN.
    #: Empty string disables mDNS advertisement entirely.
    mdns_hostname: str = "hokku"

    #: Which interface the bare "/" serves: "classic" (the original page) or
    #: "modern" (the redesigned app). A real field, not a loose config-only key,
    #: so it survives config saves (asdict/to_dict) instead of being silently dropped.
    default_ui: str = "classic"

    def cache_slug(self) -> str:
        """Path-safe fingerprint of fields that affect cached panel output."""
        payload = {
            "image_config_default": self.image_config_default.cache_slug(),
            "image_config_bw": self.image_config_bw.cache_slug(),
            "image_config_face": self.image_config_face.cache_slug(),
            "classifier_bw_detect_enabled": self.classifier_bw_detect_enabled,
            "classifier_face_detect_enabled": self.classifier_face_detect_enabled,
            "classifier_face_detect_clahe_keepout": self.classifier_face_detect_clahe_keepout,
            "crop_to_fill_threshold": self.crop_to_fill_threshold,
            "auto_rotate_fit": self.auto_rotate_fit,   # affects framing -> must invalidate cached renders
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:14]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Parse from a dict. Returns a fresh default v1 when 'version' is absent."""
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")

        if "version" not in data:
            # Unversioned (legacy or hand-written) — return default.
            return cls()

        data = _migrate(data)

        image_config_default = _image_config_from_dict(
            data.get("image_config_default"), field_path="image_config_default"
        )
        image_config_bw = _image_config_from_dict(
            data.get("image_config_bw"), field_path="image_config_bw"
        )
        image_config_face = _image_config_from_dict(
            data.get("image_config_face"), field_path="image_config_face"
        )

        _image_fields = {"image_config_default", "image_config_bw", "image_config_face"}

        kwargs: dict[str, Any] = {
            "image_config_default": image_config_default,
            "image_config_bw": image_config_bw,
            "image_config_face": image_config_face,
        }
        for f in fields(cls):
            if f.name in _image_fields:
                continue
            if f.name in data:
                kwargs[f.name] = data[f.name]

        rat = kwargs.get("refresh_image_at_time")
        if isinstance(rat, list):
            kwargs["refresh_image_at_time"] = tuple(rat)

        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Read JSON from path. exit(1) on unparseable JSON.

        Missing file or valid JSON without 'version' → writes a fresh default
        and continues (self-healing).
        """
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            logger.info("Config not found — created default v%s: %s", _CURRENT_VERSION, path)
            return cfg
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load config from %s: %s", path, e)
            sys.exit(1)

        try:
            cfg = cls.from_dict(data)
        except (TypeError, ValueError) as e:
            logger.error("Failed to parse config from %s: %s", path, e)
            sys.exit(1)

        if "version" not in data:
            # Write the default back so the next load is clean.
            cfg.save(path)
            logger.info("Unversioned config replaced with default v%s: %s", _CURRENT_VERSION, path)
        else:
            logger.info("Config loaded from: %s", path)
        return cfg

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.to_dict())
        logger.info("Config saved to: %s", path)
