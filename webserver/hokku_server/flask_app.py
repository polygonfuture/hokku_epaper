"""Flask application factory and route handlers.

Routes only — no module-level mutable globals. Live state lives in the
AppState instance passed to ``create_app()``. All route handlers read
``state.manager`` / ``state.scheduler`` / ``state.config`` at the start of
each request so they automatically pick up a hot-reloaded config.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import threading
import time as _time
from dataclasses import asdict, replace
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path

import pillow_jxl  # noqa: F401 — PIL plugin registration
import psutil
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

from hokku_server import mono_e1003, serve_log
from hokku_server.app_config import AppConfig
from hokku_server.app_state import AppState
from hokku_server.display import FULL_W, PANEL_H, TOTAL_BYTES, VISUAL_H, VISUAL_W
from hokku_server.dither_streaming_numba import NumbaStreamingDither
from hokku_server.image_abc import _transform_keepout_through_crop, transform_bboxes_to_canvas_norm
from hokku_server.image_config import _image_config_from_dict
from hokku_server.image_record import ConvertStatus
from hokku_server.image_renderer import (
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_PIXELS,
    SVG_PROBE_DIMS,
    ImageRenderer,
    open_image_for_render,
)
from hokku_server.orientation import Orientation, orientation_from_dims
from hokku_server.presets import PRESET_IMAGE_CONFIGS, PRESET_META
from hokku_server.screen_headers import parse_battery_header, parse_frame_state
from hokku_server.time_utils import calculate_sleep_seconds, format_duration_human

logger = logging.getLogger(__name__)

register_heif_opener()


def _is_self_request(remote_addr: str | None) -> bool:
    """True if a /hokku/screen/ request originates from the server host itself —
    loopback, or the machine's own LAN IP. Such requests are connectivity tests or
    health checks, NEVER a real frame, so they must not register a phantom screen
    (a headerless self-hit would otherwise persist as an "unnamed" frame with the
    server's own IP). Real frames — named, or unprovisioned with their own LAN IP —
    are unaffected."""
    if not remote_addr:
        return False
    if remote_addr in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        from hokku_server.mdns import _get_local_ip

        return remote_addr == _get_local_ip()
    except Exception:
        return False


def _resolve_template_folder(override: str | None) -> str:
    if override:
        return override
    pkg_root = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_root / "templates",
        Path("/usr/share/hokku-server/templates"),
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)
    return str(candidates[0])  # default; flask will error if missing


def _resolve_static_folder() -> Path:
    """Locate the directory that holds /hokku/static/* assets.

    Dev: <webserver_root>/static/. Installed: /usr/share/hokku-server/static/.
    """
    pkg_root = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_root / "static",
        Path("/usr/share/hokku-server/static"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _read_git_describe() -> tuple[str, str | None]:
    """Returns (version string, full commit hash or None).

    Prefers git describe (dev tree); falls back to installed package metadata.
    """
    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        describe = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--always"],  # noqa: S607 — git on PATH is expected
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode("ascii")
            .strip()
        )
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],  # noqa: S607
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode("ascii")
            .strip()
        )
        if describe:
            return describe, commit or None
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        return _pkg_version("hokku-server"), None
    except Exception:
        return "unknown", None


_REPO_URL = "https://github.com/defl/hokku_epaper"


def _busy_retry_seconds(config: AppConfig) -> int:
    return min(300, calculate_sleep_seconds(config))


def _render_version(rec) -> str:
    """A short stamp that changes whenever ANY render input for this photo changes, so the
    app's image cache-buster refetches the fresh render. Covers the colour render (the two
    ScreenImageConfig slugs) AND the mono render (which renders live, no slug) via its edit
    state. size_bytes/last_conversion_seconds alone miss crop edits → stale colour/mono
    previews (the "reset+rotate shows a stale portrait" bug)."""
    parts = [
        rec.landscape_image_config_slug or "",
        rec.portrait_image_config_slug or "",
        # mono renders live from edit state; include what _effective_mono_kwargs reads so a
        # mono-only or inherited-crop change also busts the mono URL.
        json.dumps(rec.edit_crop, sort_keys=True) if rec.edit_crop else "",
        json.dumps(rec.edit_crop_mono, sort_keys=True) if rec.edit_crop_mono else "",
        json.dumps(rec.edit_mono, sort_keys=True) if rec.edit_mono else "",
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


# Diagnostic A/B toggle state (mono_e1003_ab_toggle): flips each E1003 refresh.
# Guarded by _AB_TOGGLE_LOCK because it's the one request-mutated module global, and with
# threaded request serving two concurrent /hokku/screen/ requests would otherwise interleave
# its read-modify-write (skipping/repeating an A/B variant or racing the pinned image).
_AB_TOGGLE: dict[str, bool] = {}
_AB_TOGGLE_LOCK = threading.Lock()


def create_app(
    state: AppState,
    *,
    config_path: Path | None = None,
    template_folder: str | None = None,
) -> Flask:
    """Build the Flask app over an AppState.

    config_path is optional but required for save-config to work.
    """
    app = Flask(__name__, template_folder=_resolve_template_folder(template_folder))

    static_root = _resolve_static_folder()
    git_describe, git_hash = _read_git_describe()

    # ── Firmware-facing ────────────────────────────────────────

    @app.route("/hokku/screen/", strict_slashes=False, methods=["GET", "POST"])
    def serve_binary():
        manager = state.manager
        scheduler = state.scheduler
        config = state.config

        has_screen_name = "X-Screen-Name" in request.headers
        screen_name = request.headers.get("X-Screen-Name", "unnamed")
        screen_ip = request.remote_addr or "unknown"
        battery_mv = parse_battery_header(request.headers.get("X-Battery-mV"))
        frame_state = parse_frame_state(request.headers.get("X-Frame-State"))
        panel_type = request.headers.get("X-Panel-Type") or None

        # A headerless request from the server host itself (a connectivity test or a
        # health-check probe, never a real frame) must not register a phantom "unnamed"
        # screen. Answer 200 so the probe sees the server is alive, but don't touch the
        # scheduler. Real frames send X-Screen-Name (or, if unprovisioned, hit this from
        # their own LAN IP) and fall through to normal registration.
        if not has_screen_name and _is_self_request(request.remote_addr):
            return make_response("hokku alive", 200)

        # POST body carries the firmware log (plain text); GET has no body.
        screen_log: str | None = None
        if request.method == "POST":
            raw = request.get_data()
            if raw:
                screen_log = raw.decode("utf-8", errors="replace")

        cfg = scheduler.get_screen_config(screen_name)
        pick_orientation = cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL
        # Ground-truth capture for the serve-decision log: what THIS screen's drawer is showing
        # right now (its committed pick / pin), and the whole fleet's committed map, BEFORE we
        # pick — so a drawer≠serve mismatch or a cross-frame reshuffle is provable, not inferred.
        # Only computed when the log is enabled (the Settings toggle) — 'off' stays zero-cost.
        _log_serve = serve_log.is_enabled()
        committed_before = scheduler.committed_next_for_screen(screen_name) if _log_serve else None
        fleet_before = scheduler.committed_snapshot() if _log_serve else None
        # A pending per-screen override wins over rotation. It deliberately bypasses
        # the orientation filter — both orientations are rendered for every OK image.
        forced = scheduler.peek_next_for_screen(screen_name)
        # pick_next(screen_name) returns this frame's COMMITTED pick — de-dup + tiebreak
        # already resolved at commit time, and it's the exact value the drawer shows.
        chosen = forced if forced is not None else scheduler.pick_next(
            orientation=pick_orientation, screen_name=screen_name
        )
        # A/B-toggle diagnostic pins the image so each refresh swaps only the
        # tone treatment (A<->B), not the photo. Pins the first image served.
        # (Locked — request-mutated global under threaded serving.)
        with _AB_TOGGLE_LOCK:
            if config.mono_e1003_ab_toggle:
                if _AB_TOGGLE.get("image"):
                    chosen = _AB_TOGGLE["image"]
                elif chosen is not None:
                    _AB_TOGGLE["image"] = chosen
            else:
                _AB_TOGGLE.pop("image", None)
        sleep_seconds = calculate_sleep_seconds(config) if chosen else _busy_retry_seconds(config)

        if chosen is None:
            progress = manager.conversion_progress()
            converting = progress.total > 0
            scheduler.record_screen_call(
                screen_name,
                screen_ip,
                sleep_seconds,
                None,
                battery_mv,
                frame_state,
                log=screen_log,
                panel_type=panel_type,
            )
            if converting:
                msg, status, label = "Converting images, try again shortly", 503, "Converting"
            else:
                msg, status, label = "No images in upload directory", 404, "No images"
            resp = make_response(msg, status)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            logger.debug("%s: %s told to retry in %ss", label, screen_name, sleep_seconds)
            return resp

        # E1003 mono clients announce themselves per-request (panel_type read at
        # the top); everything else (scheduling, telemetry, headers) is shared
        # with the Spectra path. Minimal M2 hook — PanelProfile refactor later.
        if panel_type == mono_e1003.MONO_PANEL_TYPE:
            try:
                # Sharpen params for the A/B toggle + split-compare diagnostics (the
                # normal render below spells its own out, with the per-image override).
                _sharp = dict(
                    sharpen_radius=config.mono_e1003_sharpen_radius,
                    sharpen_percent=config.mono_e1003_sharpen_amount,
                    sharpen_threshold=config.mono_e1003_sharpen_threshold,
                )
                if config.mono_e1003_ab_toggle:
                    # Each refresh alternates A (uniform 1.40) <-> B (measured).
                    # (Locked read-modify-write — request-mutated global under threading.)
                    with _AB_TOGGLE_LOCK:
                        _AB_TOGGLE["measured"] = not _AB_TOGGLE.get("measured", False)
                        meas = _AB_TOGGLE["measured"]
                    binary = mono_e1003.render_mono_bin(
                        manager.original_path(chosen),
                        use_measured_ramp=meas,
                        darken_gamma=config.mono_e1003_darken_gamma if meas else 1.40,
                        shadow_lift=config.mono_e1003_shadow_lift,
                        **_sharp,
                    )
                    logger.info("A/B toggle -> %s", "B (measured)" if meas else "A (uniform)")
                elif config.mono_e1003_split_compare:
                    binary = mono_e1003.render_split_compare(
                        manager.original_path(chosen),
                        gamma_a=1.40,
                        gamma_b=config.mono_e1003_darken_gamma,
                        shadow_lift=config.mono_e1003_shadow_lift,
                        **_sharp,
                    )
                else:
                    # Per-image tone override (Photo editor) layered on the panel default.
                    # The frame's configured orientation drives the mono framing: a portrait
                    # E1003 gets portrait-composed content rotated into the landscape buffer.
                    fp = cfg.orientation == Orientation.PORTRAIT
                    binary = mono_e1003.render_mono_bin(
                        manager.original_path(chosen),
                        **_effective_mono_kwargs(
                            config, manager.status(chosen), frame_portrait=fp,
                            crop_anchor_bboxes_norm=_face_anchor_for(
                                state, manager.status(chosen)
                            ),
                        ),
                    )
            except Exception:
                logger.exception("mono16_e1003 render failed for %s", chosen)
                binary = None
        else:
            binary = manager.panel_bytes_for_orientation(chosen, cfg.orientation)
        if binary is None:
            # Cache missing (not yet rendered for this orientation) — tell screen to retry.
            sleep_seconds = _busy_retry_seconds(config)
            scheduler.record_screen_call(
                screen_name,
                screen_ip,
                sleep_seconds,
                None,
                battery_mv,
                frame_state,
                log=screen_log,
                panel_type=panel_type,
            )
            resp = make_response("Cached binary missing, try again shortly", 503)
            resp.headers["X-Sleep-Seconds"] = str(sleep_seconds)
            return resp

        scheduler.mark_served(chosen, screen_name=screen_name)
        scheduler.record_screen_call(
            screen_name,
            screen_ip,
            sleep_seconds,
            chosen,
            battery_mv,
            frame_state,
            log=screen_log,
            panel_type=panel_type,
        )
        # Durable serve-decision record: what the drawer showed vs what actually served, plus the
        # whole fleet's committed map before/after — the ground truth for diagnosing drawer≠serve.
        if _log_serve:
            serve_log.record({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "screen": screen_name,
                "ip": screen_ip,
                "committed_before": committed_before,
                "pin": forced,
                "chosen": chosen,
                "served": chosen,
                "drawer_matched_serve": chosen == committed_before,
                "fleet_before": fleet_before,
                "fleet_after": scheduler.committed_snapshot(),
            })
        logger.debug("Serving: %s to %s (sleep_seconds=%s)", chosen, screen_name, sleep_seconds)

        response = make_response(binary)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["X-Sleep-Seconds"] = str(sleep_seconds)
        response.headers["X-Server-Time-Epoch"] = str(int(_time.time()))
        response.headers["Content-Disposition"] = "attachment; filename=hokku.bin"
        return response

    # ── Web GUI ────────────────────────────────────────────────

    def _default_ui() -> str:
        # Which interface the bare "/" opens: "classic" (the original page) or
        # "modern" (the redesigned app). Read as an optional key from config.json;
        # AppConfig ignores keys it doesn't know, so this needs no schema change.
        if config_path:
            try:
                with open(config_path) as f:
                    choice = str(json.load(f).get("default_ui", "classic")).lower()
                return "modern" if choice == "modern" else "classic"
            except (OSError, json.JSONDecodeError):
                pass
        return "classic"

    @app.route("/")
    def root():
        return redirect("/hokku/app" if _default_ui() == "modern" else "/hokku/ui")

    @app.route("/hokku/ui")
    def web_gui():
        return render_template("index.html", visual_w=VISUAL_W, visual_h=VISUAL_H)

    @app.route("/hokku/app")
    def modern_gui():
        # the redesigned no-build web app (webserver/static/app/); its asset links
        # are absolute so it loads correctly from this short URL and from "/".
        return send_from_directory(static_root, "app/index.html")

    @app.route("/hokku/static/<path:filename>")
    def static_asset(filename: str):
        # send_from_directory rejects path-traversal automatically.
        return send_from_directory(static_root, filename)

    # ── API: image data ────────────────────────────────────────

    @app.route("/hokku/api/original/<path:name>")
    def api_original(name: str):
        manager = state.manager
        try:
            path = manager.original_path(name)
        except FileNotFoundError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path)

    @app.route("/hokku/api/display/<path:name>")
    def api_display(name: str):
        # Browser-safe rendition of the ORIGINAL photo (detail Original view + editor crop
        # source). Web-decodable formats pass through raw; tiff/heic/heif/jxl/svg serve a cached
        # re-encoded/rasterised JPEG. /original stays the raw bytes (used by "Download original").
        path = state.manager.display_path_for(name)
        if path is None or not path.is_file():
            abort(404)
        return send_file(path)   # correct content-type for both passthrough + JPEG

    @app.route("/hokku/api/dithered/<path:name>")
    def api_dithered(name: str):
        # ?mono=1 → the E1003 render (what a mono frame actually shows), so the UI can
        # preview a photo the way it looks on the E1003 instead of the colour dither.
        if request.args.get("mono"):
            rec = state.manager.status(name)
            if rec is None:
                abort(404)
            # ?portrait=1 previews the render for a PORTRAIT-mounted E1003 (the frame's
            # configured orientation); default landscape. The client passes the orientation
            # of the frame the photo is on so the thumbnail matches the panel.
            fp = bool(request.args.get("portrait"))
            try:
                binary = mono_e1003.render_mono_bin(
                    state.manager.original_path(name),
                    **_effective_mono_kwargs(
                        state.config, rec, frame_portrait=fp,
                        crop_anchor_bboxes_norm=_face_anchor_for(state, rec),
                    ),
                )
            except FileNotFoundError:
                abort(404)
            except Exception:
                logger.exception("mono dithered render failed for %r", name)
                abort(500)
            # Always decoded UPRIGHT and shaped to the frame's orientation, so a mono
            # preview is interchangeable with the colour one (?orient=) — the E1003's
            # landscape wire buffer, and the 90° rotation portrait content is stored with,
            # stay an internal detail. Every client surface wants this: card, tile, modal,
            # editor.
            buf = io.BytesIO()
            cp = _mono_content_portrait(state.config, rec, frame_portrait=fp)
            mono_e1003.mono_bin_to_upright_image(binary, cp).save(buf, format="PNG")
            return _png_response(buf.getvalue())
        # ?orient=landscape|portrait → the colour render at a SPECIFIC frame orientation
        # (the Frames drawer passes the frame's orientation so its card mirrors the panel);
        # omitted → the photo's own effective orientation (gallery/editor default).
        orient = request.args.get("orient")
        if orient in ("landscape", "portrait"):
            png = state.manager.preview_png_for_orientation(name, Orientation(orient))
        else:
            png = state.manager.preview_png(name)
        if png is None:
            abort(404)
        return _png_response(png)

    @app.route("/hokku/api/thumbnail/<path:name>")
    def api_thumbnail(name: str):
        jpg = state.manager.thumbnail_jpg(name)
        if jpg is None:
            abort(404)
        resp = make_response(jpg)
        resp.headers["Content-Type"] = "image/jpeg"
        return resp

    # ── API: image management ──────────────────────────────────

    @app.route("/hokku/api/upload", methods=["POST"])
    def api_upload():
        manager = state.manager
        files = request.files.getlist("file") or request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files in upload"}), 400
        saved, skipped = [], []
        for f in files:
            if not f or not f.filename:
                continue
            name = secure_filename(f.filename)
            if not name:
                skipped.append({"name": f.filename, "reason": "invalid filename"})
                continue
            ext = Path(name).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                skipped.append({"name": name, "reason": f"unsupported extension {ext}"})
                continue
            data = f.read()
            # Header-only dimension probe — cheap and safe even for bomb PNGs.
            # Reject before writing to disk so we never decode a giant buffer.
            # SVG: skip PIL probe; resvg always renders within screen resolution
            # so the pixel budget is never exceeded.
            if ext == ".svg":
                w, h = SVG_PROBE_DIMS
            else:
                try:
                    with Image.open(io.BytesIO(data)) as probe:
                        w, h = probe.size
                except Image.DecompressionBombError:
                    skipped.append(
                        {
                            "name": name,
                            "reason": f"image too large; cap {MAX_UPLOAD_PIXELS:,} px",
                        }
                    )
                    continue
                except (UnidentifiedImageError, OSError) as e:
                    logger.exception("Upload error for %r: %s: %s", name, type(e).__name__, e)
                    skipped.append({"name": name, "reason": "unreadable image"})
                    continue
            if w * h > MAX_UPLOAD_PIXELS:
                skipped.append(
                    {
                        "name": name,
                        "reason": f"image too large ({w}x{h}); cap {MAX_UPLOAD_PIXELS:,} px",
                    }
                )
                continue
            try:
                manager.add(name, data)
                saved.append(name)
            except FileExistsError:
                skipped.append({"name": name, "reason": "already exists; remove to replace"})
            except (OSError, ValueError) as e:
                logger.exception("Error adding %r: %s: %s", name, type(e).__name__, e)
                skipped.append({"name": name, "reason": str(e)})
        return jsonify({"saved": saved, "skipped": skipped})

    @app.route("/hokku/api/upload_draft", methods=["POST"])
    def api_upload_draft():
        """Upload a single image as an inert DRAFT for the per-image editor.

        Same validation as /upload, but the image is registered as a DRAFT (it never
        auto-converts or joins rotation) and a unique name is returned for the editor.
        """
        f = (request.files.getlist("file") or request.files.getlist("files") or [None])[0]
        if f is None or not f.filename:
            return jsonify({"error": "No file in upload"}), 400
        name = secure_filename(f.filename)
        if not name:
            return jsonify({"error": "invalid filename"}), 400
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            return jsonify({"error": f"unsupported extension {ext}"}), 400
        data = f.read()
        if ext == ".svg":
            w, h = SVG_PROBE_DIMS
        else:
            try:
                with Image.open(io.BytesIO(data)) as probe:
                    w, h = probe.size
            except Image.DecompressionBombError:
                return jsonify({"error": f"image too large; cap {MAX_UPLOAD_PIXELS:,} px"}), 400
            except (UnidentifiedImageError, OSError):
                return jsonify({"error": "unreadable image"}), 400
        if w * h > MAX_UPLOAD_PIXELS:
            return jsonify({"error": f"image too large ({w}x{h}); cap {MAX_UPLOAD_PIXELS:,} px"}), 400
        # Retry with a suffixed name if the base name is taken, so drafts never collide.
        draft_name, n = name, 1
        while True:
            try:
                state.manager.add_draft(draft_name, data)
                return jsonify({"name": draft_name})
            except FileExistsError:
                draft_name = f"{Path(name).stem}-draft{n}{ext}"
                n += 1
                if n > 100:
                    return jsonify({"error": "could not allocate a draft name"}), 500
            except (OSError, ValueError) as e:
                return jsonify({"error": str(e)}), 400

    @app.route("/hokku/api/image/<path:name>", methods=["DELETE"])
    def api_delete(name: str):
        try:
            state.manager.remove(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        # Reconcile the scheduler immediately: if the deleted image was a frame's committed
        # "up next" (or a pin), drop it and re-pick that frame now, so the drawer stops showing
        # a dead thumbnail instead of waiting for the next serve to notice.
        state.scheduler.library_changed()
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/retry", methods=["POST"])
    def api_retry(name: str):
        try:
            state.manager.retry(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        return jsonify({"ok": True})

    @app.route("/hokku/api/image/<path:name>/suggested_config", methods=["GET"])
    def api_suggested_config(name: str):
        """The editor's initial state for an image: the classifier's per-image
        decision, detection results, and the source dimensions/orientation."""
        rec = state.manager.status(name)
        if rec is None:
            return jsonify({"error": f"image {name!r} not found"}), 404
        try:
            path = state.manager.original_path(name)
            with open_image_for_render(path) as img:
                w, h = img.size
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        except (UnidentifiedImageError, OSError) as e:
            return jsonify({"error": f"could not read image: {e}"}), 400
        decision = state.classifier.decision_for(path, rec.original_sha1)
        obs = state.classifier.observations_for(rec.original_sha1)
        face_bboxes = [[b.x, b.y, b.w, b.h] for b in (obs.face_bboxes or ())]
        # Which of the three pipelines the classifier landed on. Mirrors
        # ImageClassifier._classify's B&W > Face > Default order, read from the cached
        # observations + current config, so the editor can show the auto-decision.
        cfg = state.config
        if cfg.classifier_bw_detect_enabled and obs.is_bw:
            pipeline = "bw"
        elif cfg.classifier_face_detect_enabled and obs.face_bboxes:
            pipeline = "face"
        else:
            pipeline = "default"
        return jsonify(
            {
                "image_config": asdict(decision.image_config),
                "crop_to_fill_threshold": decision.crop_to_fill_threshold,
                "is_bw": obs.is_bw,
                "face_bboxes": face_bboxes,
                "pipeline": pipeline,
                "faces_protected": bool(decision.clahe_keepout_bboxes),
                # the per-image editor's SAVED edit (None if never edited) so re-opening
                # restores the user's config + crop. image_config above stays the
                # classifier's live "Auto" baseline (the Reset/Auto target).
                "edit_image_config": rec.edit_image_config,
                "edit_crop": rec.edit_crop,
                # per-image E1003 mono tone override (None = inherits the panel default)
                "edit_mono": rec.edit_mono,
                # per-image E1003 crop override (None = the mono panel follows edit_crop)
                "edit_crop_mono": rec.edit_crop_mono,
                "orientation": orientation_from_dims(w, h).value,
                "source_w": w,
                "source_h": h,
            }
        )

    @app.route("/hokku/api/image/<path:name>/edit", methods=["POST"])
    def api_edit(name: str):
        """Commit the editor's per-image config + crop and queue a (re)render.

        Body: {image: ImageConfig dict | null, edit_crop: {rotation_quarters, rect,
        target} | null, mono: {mono knob set} | null}. A null image means "use the
        classifier's own decision". ``mono`` is the per-image E1003 tone override
        (short knob names); null clears it (inherit the panel default). It updates
        independently of the colour pipeline — no colour re-render.
        """
        if state.manager.status(name) is None:
            return jsonify({"error": f"image {name!r} not found"}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        image_blob = body.get("image")
        edit_crop = body.get("edit_crop")
        has_mono = "mono" in body
        mono_blob = body.get("mono")
        has_crop_mono = "edit_crop_mono" in body
        crop_mono_blob = body.get("edit_crop_mono")
        if image_blob is not None:
            if not isinstance(image_blob, dict):
                return jsonify({"error": "image must be an ImageConfig object or null"}), 400
            try:
                _image_config_from_dict(image_blob)  # validate only
            except (TypeError, ValueError) as e:
                return jsonify({"error": f"invalid image config: {e}"}), 400
        if edit_crop is not None and not isinstance(edit_crop, dict):
            return jsonify({"error": "edit_crop must be an object or null"}), 400
        if has_mono and mono_blob is not None and not isinstance(mono_blob, dict):
            return jsonify({"error": "mono must be an object or null"}), 400
        if has_crop_mono and crop_mono_blob is not None and not isinstance(crop_mono_blob, dict):
            return jsonify({"error": "edit_crop_mono must be an object or null"}), 400
        try:
            state.manager.commit_edit(name, image_blob, edit_crop)
            # Only touch the mono overrides when the caller sent the field, so a
            # colour-only save never clears an existing per-image mono edit/crop.
            if has_mono:
                state.manager.set_edit_mono(name, mono_blob)
            if has_crop_mono:
                state.manager.set_edit_crop_mono(name, crop_mono_blob)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "name": name})

    @app.route("/hokku/api/show_next/<path:name>", methods=["POST"])
    def api_show_next(name: str):
        rec = state.manager.status(name)
        if rec is None:
            return jsonify({"error": f"image {name!r} not found"}), 404
        if rec.convert_status != ConvertStatus.OK:
            return jsonify(
                {"error": f"image {name!r} is not ready (status: {rec.convert_status})"}
            ), 409
        try:
            state.scheduler.set_next(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True, "next_image": name})

    @app.route("/hokku/api/clear_cache", methods=["POST"])
    def api_clear_cache():
        state.manager.clear_caches()
        state.manager.sync()  # kick off reconversion immediately
        return jsonify({"ok": True})

    @app.route("/hokku/api/classifier/clear", methods=["POST"])
    def api_classifier_clear():
        """Wipe all cached classifier observations (is_bw / has_face) and trigger re-sync.

        Deletes image_classifier.json and kicks off immediate re-classification.
        Already-rendered panel .bin files are NOT touched — they are keyed by
        ScreenImageConfig slug and remain valid unless the classification result changes.
        """
        state.classifier.clear_cache()
        state.manager.sync()
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>", methods=["DELETE"])
    def api_screen_delete(name: str):
        """Remove a screen's telemetry and serve-stats records.

        The screen will re-appear automatically the next time it connects.
        """
        state.scheduler.remove_screen(name)
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>/config", methods=["PATCH"])
    def api_screen_config(name: str):
        """Patch per-screen config (orientation and/or orientation filter)."""
        body = request.get_json(silent=True) or {}
        current = state.scheduler.get_screen_config(name)
        updates: dict = {}

        if "orientation" in body:
            raw = body.get("orientation")
            if raw not in ("landscape", "portrait"):
                return jsonify({"error": "orientation must be 'landscape' or 'portrait'"}), 400
            updates["orientation"] = Orientation(raw)

        if "filter_by_orientation" in body:
            val = body.get("filter_by_orientation")
            if not isinstance(val, bool):
                return jsonify({"error": "filter_by_orientation must be a boolean"}), 400
            updates["filter_by_orientation"] = val

        if "display_name" in body:
            val = body.get("display_name")
            if val is None:
                val = ""   # explicit null == reset to the provisioned name
            if not isinstance(val, str):
                return jsonify({"error": "display_name must be a string"}), 400
            updates["display_name"] = val.strip()[:64]

        if updates:
            state.scheduler.set_screen_config(name, replace(current, **updates))
            # display_name is cosmetic; only re-sync when a render-affecting
            # field (orientation / filter) changed.
            if "orientation" in updates or "filter_by_orientation" in updates:
                state.manager.sync()
        return jsonify({"ok": True})

    @app.route("/hokku/api/screens/<string:name>/show_next", methods=["POST", "DELETE"])
    def api_screen_show_next(name: str):
        """Force a specific image onto ONE screen's next refresh (POST), or cancel
        a pending per-screen override (DELETE, idempotent).

        Unlike the global /hokku/api/show_next, this targets a single frame: the
        override is consumed when THAT frame next checks in. It bypasses the
        screen's orientation filter (both orientations are always rendered)."""
        if request.method == "DELETE":
            state.scheduler.clear_next_for_screen(name)
            return jsonify({"ok": True})

        if name not in state.scheduler.screens():
            return jsonify({"error": f"screen {name!r} not known"}), 404
        body = request.get_json(silent=True) or {}
        image = body.get("image")
        if not isinstance(image, str) or not image:
            return jsonify({"error": "body must be {'image': <name>}"}), 400
        rec = state.manager.status(image)
        if rec is None:
            return jsonify({"error": f"image {image!r} not found"}), 404
        if rec.convert_status != ConvertStatus.OK:
            return jsonify(
                {"error": f"image {image!r} is not ready (status: {rec.convert_status})"}
            ), 409
        try:
            state.scheduler.set_next_for_screen(name, image)
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"ok": True, "screen": name, "next_image": image})

    @app.route("/hokku/api/screens/<string:name>/skip", methods=["POST"])
    def api_screen_skip(name: str):
        """HARD-skip this frame's current 'up next': send it to the back of THIS frame's line
        and re-pick a fresh up-next for this frame only (per-frame — other frames unaffected).
        Returns the new up-next image. Takes effect on the frame's next wake."""
        if name not in state.scheduler.screens():
            return jsonify({"error": f"screen {name!r} not known"}), 404
        new_next = state.scheduler.skip_next(name)
        return jsonify({"ok": True, "screen": name, "next_image": new_next})

    @app.route("/hokku/api/scrub", methods=["POST"])
    def api_scrub():
        """Remove stale-slug panel/preview files immediately (preserves thumbs)."""
        state.manager.scrub_stale_cache()
        return jsonify({"ok": True})

    # ── API: status + config ───────────────────────────────────

    @app.route("/hokku/api/status")
    def api_status():
        manager = state.manager
        scheduler = state.scheduler

        records = manager.list()
        progress = manager.conversion_progress()
        last = scheduler.last_served()
        classifier = state.classifier
        upload_files = []
        failed_files = []
        for r in records:
            obs = classifier.observations_for(r.original_sha1) if r.original_sha1 else None
            entry = {
                "name": r.name,
                "dithered": r.convert_status == ConvertStatus.OK,
                "status": r.convert_status,
                "error": r.convert_error,
                "size_bytes": r.original_size_bytes,
                # Source-content token — changes on re-upload / content change (reconcile keys on
                # sha1). The app busts the /display URL on this (NOT render_version: the display
                # rendition is the UNCROPPED source, unaffected by crop/tone edits).
                "src_token": (r.original_sha1[:12] if r.original_sha1 else str(r.original_size_bytes or 0)),
                "added_at": r.added_at,  # first-uploaded time; the app sorts newest-first by this
                "image_width": r.image_width,
                "image_height": r.image_height,
                "dimension_unit": "pt" if Path(r.name).suffix.lower() == ".svg" else "px",
                "native_orientation": r.native_orientation.value
                if r.convert_status == ConvertStatus.OK
                else None,
                # display orientation: the editor's crop target when edited, else native; plus
                # whether the image carries an edit crop, so the app lays out the right aspect.
                # Reportable while an edited image re-renders (target is known from the crop,
                # no OK needed) — only the native fallback requires an ok-status image.
                "effective_orientation": r.effective_orientation.value
                if (r.convert_status == ConvertStatus.OK or (r.edit_crop and r.edit_crop.get("target")))
                else None,
                "edited": r.edit_crop is not None,
                # The shape each editor crop was authored FOR (None = no such crop). A crop only
                # applies on a mount of its own shape (ImageRecord.crop_for / _mono_crop_for), and
                # the crop box is locked to the panel aspect, so "crop applies here" == "exact
                # fit". The frame-preview chip needs both to say that without guessing: colour
                # frames consult crop_target, mono frames try crop_target_mono then crop_target.
                "crop_target": (r.edit_crop or {}).get("target"),
                "crop_target_mono": (r.edit_crop_mono or {}).get("target"),
                # A render-version stamp that changes whenever ANY render input changes
                # (colour slugs + mono edit state). The app's cache-buster keys on this so an
                # edit always fetches the fresh render — size_bytes/last_conversion_seconds
                # alone do NOT change reliably on a crop edit (stale colour/mono previews).
                "render_version": _render_version(r),
                "last_conversion_seconds": r.last_conversion_seconds,
                "is_bw": obs.is_bw if obs else None,
                "face_bboxes": [[b.x, b.y, b.w, b.h] for b in obs.face_bboxes]
                if (obs and obs.face_bboxes)
                else [],
            }
            upload_files.append(entry)
            if r.convert_status == ConvertStatus.FAILED:
                failed_files.append(
                    {"name": r.name, "error": r.convert_error, "size_bytes": r.original_size_bytes}
                )

        ready_count = sum(1 for r in records if r.convert_status == ConvertStatus.OK)
        serve_data: dict[str, dict] = {}
        for n, s in scheduler.stats().items():
            serve_data[n] = {
                "show_index": s.show_index,
                "last_request": (
                    datetime.fromtimestamp(s.last_served_at).isoformat(timespec="seconds")
                    if s.last_served_at
                    else None
                ),
                "total_show_count": s.total_show_count,
                "total_show_minutes": s.total_show_minutes,
                "total_show_formatted": format_duration_human(s.total_show_minutes),
            }

        screens_payload: dict[str, dict] = {}
        screen_peek_orientations: set[Orientation] = set()
        for sname, t in scheduler.screens().items():
            next_update_at = None
            if t.last_seen_at and t.last_sleep_seconds:
                next_update_at = datetime.fromtimestamp(
                    t.last_seen_at + t.last_sleep_seconds
                ).isoformat(timespec="seconds")
            scfg = scheduler.get_screen_config(sname)
            peek_orientation = (
                scfg.orientation if scfg.filter_by_orientation else Orientation.NEUTRAL
            )
            screen_peek_orientations.add(peek_orientation)
            screens_payload[sname] = {
                "ip": t.ip,
                "request_count": t.request_count,
                "last_seen": (
                    datetime.fromtimestamp(t.last_seen_at).isoformat(timespec="seconds")
                    if t.last_seen_at
                    else None
                ),
                "last_sleep_seconds": t.last_sleep_seconds,
                "last_served": t.last_served,
                "battery_mv": t.battery_mv,
                "battery_percent": t.battery_percent,
                "battery_seen_at": (
                    datetime.fromtimestamp(t.battery_seen_at).isoformat(timespec="seconds")
                    if t.battery_seen_at
                    else None
                ),
                "next_update_at": next_update_at,
                "state": t.frame_state,
                "panel_type": t.panel_type,
                "display_name": scfg.display_name or None,
                "orientation": scfg.orientation,
                "filter_by_orientation": scfg.filter_by_orientation,
                "next_override": scheduler.peek_next_for_screen(sname),
                # The image this frame will actually serve next (pin if pending, else the
                # committed rotation pick). The drawer reads this so "up next" == the serve.
                "next": scheduler.committed_next_for_screen(sname),
                "last_log": t.last_log or None,
                "last_log_at": (
                    datetime.fromtimestamp(t.last_log_at).isoformat(timespec="seconds")
                    if t.last_log_at
                    else None
                ),
            }

        disk = manager.cache_disk_info()
        return jsonify(
            {
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "upload_size": len(records),
                "pool_size": ready_count,
                "pool_files": [r.name for r in records if r.convert_status == ConvertStatus.OK],
                "upload_files": upload_files,
                "failed_files": failed_files,
                "serve_data": serve_data,
                "screens": screens_payload,
                "last_served": last[0] if last else None,
                "converting": 1 if progress.current_name or progress.done < progress.total else 0,
                "converting_name": progress.current_name,
                "converting_done": progress.done,
                "converting_total": progress.total,
                "converting_eta_seconds": manager.estimate_remaining_seconds(),
                "next_images": {
                    o.value: scheduler.peek_next(orientation=o)
                    for o in screen_peek_orientations
                    if scheduler.peek_next(orientation=o) is not None
                },
                "cache_used_bytes": disk["cache_used_bytes"],
                "disk_free_bytes": disk["disk_free_bytes"],
                "image_worker_count_resolved": state.manager.resolved_worker_count,
                "cpu_cores": os.cpu_count(),
                "memory_available_gb": round(psutil.virtual_memory().available / 1e9, 1),
            }
        )

    @app.route("/hokku/api/config", methods=["GET"])
    def api_config_get():
        presets = {}
        for name, p in PRESET_IMAGE_CONFIGS.items():
            meta = PRESET_META.get(name, {})
            presets[name] = {
                **asdict(p),
                "label": meta.get("label", name),
                "description": meta.get("description", ""),
            }
        return jsonify(
            {
                "config": state.config.to_dict(),
                "config_defaults": AppConfig().to_dict(),
                "dither_presets": presets,
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "panel": {"visual_w": VISUAL_W, "visual_h": VISUAL_H, "total_bytes": TOTAL_BYTES},
                "git_describe": git_describe,
                "commit_url": f"{_REPO_URL}/commit/{git_hash}" if git_hash else None,
                "repo_url": _REPO_URL,
            }
        )

    @app.route("/hokku/api/config", methods=["POST"])
    def api_config_post():
        if config_path is None:
            return jsonify({"error": "server started without a config_path; cannot save"}), 500
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        try:
            merged = {**state.config.to_dict(), **body}
            new_cfg = AppConfig.from_dict(merged)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid config: {e}"}), 400
        try:
            new_cfg.save(config_path)
        except OSError as e:
            return jsonify({"error": f"failed to write config: {e}"}), 500
        try:
            state.reload(new_cfg)
        except ValueError as e:
            return jsonify({"error": f"reload failed: {e}"}), 400
        logger.info("Config saved and reloaded in-process")
        return jsonify({"ok": True, "restarting": False})

    @app.route("/hokku/api/dither/preview", methods=["POST"])
    def api_dither_preview():
        """Render a one-off dithered preview for a given image + image_config.

        Body: {name: str, image: ImageConfig dict}. Returns PNG bytes.
        The ``X-Face-Bboxes`` response header carries face bboxes already
        transformed into the rendered preview's coordinate space (JSON list
        of [x, y, w, h] tuples, each normalised 0..1 against the preview
        image).
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        name = body.get("name")
        image_blob = body.get("image")
        if not name or not isinstance(image_blob, dict):
            return jsonify({"error": "expected {name, image}"}), 400
        try:
            path = state.manager.original_path(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404
        try:
            cfg = _image_config_from_dict(image_blob)
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid image config: {e}"}), 400

        # Look up cached face bboxes (original-image normalised) so we can map
        # them onto the rendered preview's coordinate space below.
        records = {r.name: r for r in state.manager.list()}
        record = records.get(name)
        face_bboxes_orig: tuple = ()
        if record and record.original_sha1:
            obs = state.classifier.observations_for(record.original_sha1)
            if obs and obs.face_bboxes:
                face_bboxes_orig = obs.face_bboxes

        use_clahe_keepout = body.get(
            "clahe_keepout", state.config.classifier_face_detect_clahe_keepout
        )
        keepout = face_bboxes_orig if (face_bboxes_orig and use_clahe_keepout) else None

        # Optional per-image edit (from the editor): an explicit target orientation
        # (fixes portrait drafts previewing sideways), a preview size, and a
        # crop/rotation to apply before dithering.
        orientation_raw = body.get("orientation")
        render_orientation: Orientation | None = None
        if isinstance(orientation_raw, str):
            try:
                candidate = Orientation(orientation_raw)
            except ValueError:
                candidate = None
            if candidate in (Orientation.LANDSCAPE, Orientation.PORTRAIT):
                render_orientation = candidate

        rotation_quarters = int(body.get("rotation") or 0)
        crop_raw = body.get("crop")
        crop_rect: tuple[float, float, float, float] | None = None
        if isinstance(crop_raw, (list, tuple)) and len(crop_raw) == 4:
            try:
                crop_rect = tuple(float(v) for v in crop_raw)  # type: ignore[assignment]
            except (TypeError, ValueError):
                crop_rect = None

        max_side_px = max(64, min(1600, int(body.get("max_side_px") or 800)))

        logger.debug("Preview: %r", name)
        with open_image_for_render(path) as img:
            orig_w, orig_h = img.size
            if render_orientation is None:
                # No explicit target: use the converted native orientation when we
                # have it, else derive from dimensions (works on drafts, no assert).
                if (
                    record is not None
                    and record.convert_status == ConvertStatus.OK
                    and record.native_orientation != Orientation.NEUTRAL
                ):
                    render_orientation = record.native_orientation
                else:
                    render_orientation = orientation_from_dims(orig_w, orig_h)
            png = ImageRenderer(NumbaStreamingDither()).render_preview_png(
                img,
                cfg,
                render_orientation,
                max_side_px=max_side_px,
                clahe_keepout_bboxes_norm=keepout,
                rotation_quarters=rotation_quarters,
                crop_rect=crop_rect,
            )
        logger.debug("Preview done: %r", name)

        # Map face bboxes into the preview's coordinate space for the editor's
        # keepout overlay — through the crop/rotation when one is applied.
        if rotation_quarters or crop_rect:
            face_in_crop = (
                _transform_keepout_through_crop(face_bboxes_orig, rotation_quarters, crop_rect)
                if face_bboxes_orig
                else ()
            )
            rw, rh = (orig_h, orig_w) if rotation_quarters % 2 == 1 else (orig_w, orig_h)
            cw = max(1, round((crop_rect[2] if crop_rect else 1.0) * rw))
            ch = max(1, round((crop_rect[3] if crop_rect else 1.0) * rh))
            # the crop pixels are already baked into (cw, ch); the same single threshold
            # governs the crop's framing as any other photo's, so pass it here too.
            canvas_bboxes = transform_bboxes_to_canvas_norm(
                face_in_crop, cw, ch, render_orientation, FULL_W, PANEL_H,
                state.config.crop_to_fill_threshold,
            )
        else:
            canvas_bboxes = transform_bboxes_to_canvas_norm(
                face_bboxes_orig,
                orig_w,
                orig_h,
                render_orientation,
                FULL_W,
                PANEL_H,
                state.config.crop_to_fill_threshold,
            )

        resp = _png_response(png)
        resp.headers["X-Face-Bboxes"] = json.dumps([list(b) for b in canvas_bboxes])
        return resp

    @app.route("/hokku/api/dither/preview_mono", methods=["POST"])
    def api_dither_preview_mono():
        """Render a live E1003 (mono16) preview for the mono tone editor.

        Body: {name: str, mono: {use_measured_ramp, darken_gamma,
        sharpen_amount, sharpen_radius, sharpen_threshold, shadow_lift},
        max_side_px?}. Renders through the real wire pipeline and decodes
        via the PERCEIVED calibrated ramp, so the preview shows what the
        panel will actually display (compressed blacks/whites included).
        Returns PNG bytes.
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        name = body.get("name")
        mono = body.get("mono")
        if not name or not isinstance(mono, dict):
            return jsonify({"error": "expected {name, mono}"}), 400
        try:
            path = state.manager.original_path(name)
        except FileNotFoundError:
            return jsonify({"error": f"image {name!r} not found"}), 404

        # Editor crop + rotation for the mono appearance (same wire shape as the colour
        # /dither/preview endpoint): rotation = integer quarters, crop = normalized
        # [x, y, w, h] in the rotated frame. Omitted -> the auto fit/letterbox path.
        rotation_quarters = int(body.get("rotation") or 0)
        crop_raw = body.get("crop")
        crop_rect = None
        if isinstance(crop_raw, (list, tuple)) and len(crop_raw) == 4:
            try:
                crop_rect = tuple(float(v) for v in crop_raw)
            except (TypeError, ValueError):
                crop_rect = None

        # All knobs are clamped again inside render_mono_bin; the .get chain
        # just falls back to the module defaults for anything omitted.
        try:
            common = dict(
                sharpen_radius=float(mono.get("sharpen_radius", mono_e1003.SHARPEN_RADIUS)),
                sharpen_percent=int(mono.get("sharpen_amount", mono_e1003.SHARPEN_PERCENT)),
                sharpen_threshold=int(mono.get("sharpen_threshold", mono_e1003.SHARPEN_THRESHOLD)),
            )
            frame_portrait = bool(body.get("frame_portrait"))   # the E1003 frame's orientation
            crop_kw = dict(rotation_quarters=rotation_quarters, crop_rect=crop_rect,
                           frame_portrait=frame_portrait,
                           crop_to_fill_threshold=float(getattr(state.config, "crop_to_fill_threshold", 0.0)))
            profile = mono.get("profile", "faithful")
            max_side_px = max(64, min(1600, int(body.get("max_side_px") or 900)))
            # dtcore profiles (B&W Contrast / Custom) render at PREVIEW resolution — the
            # expensive local-laplacian runs on ~4x fewer pixels than the native panel,
            # so the live editor stays responsive. The frame-serve/save path is untouched
            # (still full-res render_mono_bin); a preview may differ marginally, as it
            # already did after the old post-render downscale.
            if profile in ("bw_contrast", "custom"):
                dtcore = BW_CONTRAST_DTCORE if profile == "bw_contrast" else _build_dtcore(mono.get)
                # upright=True: the editor preview always shows content the right way up
                # (un-rotates portrait content composed sideways into the landscape buffer).
                img = mono_e1003.render_mono_preview_image(path, dtcore=dtcore, max_side=max_side_px, upright=True, **common, **crop_kw)
            else:  # faithful (legacy ramp path) — full-res wire render, then perceived decode
                binary = mono_e1003.render_mono_bin(
                    path, **common, **crop_kw,
                    use_measured_ramp=bool(mono.get("use_measured_ramp", mono_e1003.USE_MEASURED_RAMP)),
                    darken_gamma=float(mono.get("darken_gamma", mono_e1003.MAX_DARKEN_GAMMA)),
                    shadow_lift=float(mono.get("shadow_lift", 0.0)),
                    black_point=float(mono.get("black_point", 0.0)),
                    white_point=float(mono.get("white_point", 0.0)),
                    clarity=float(mono.get("clarity", 0.0)),
                    contrast=float(mono.get("contrast", mono_e1003.CONTRAST_GAIN)),
                    midtone=float(mono.get("midtone", 1.0)),
                    highlights=float(mono.get("highlights", 0.0)),
                    shadows=float(mono.get("shadows", 0.0)),
                )
                # upright decode: un-rotate portrait content for the UI preview. Pass the
                # RAW source dims — _mono_content_portrait derives the post-crop orientation
                # itself (via effective_cropped_dims), matching render_mono_bin exactly.
                with Image.open(path) as _im:
                    _sw, _sh = _im.width, _im.height
                _cp = mono_e1003._mono_content_portrait(_sw, _sh, rotation_quarters, crop_rect, frame_portrait)
                img = mono_e1003.mono_bin_to_upright_image(binary, _cp)
                if max(img.size) > max_side_px:
                    scale = max_side_px / max(img.size)
                    img = img.resize(
                        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                        Image.LANCZOS,
                    )
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"invalid mono config: {e}"}), 400
        except Exception:
            logger.exception("mono preview render failed for %r", name)
            return jsonify({"error": "render failed"}), 500

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return _png_response(buf.getvalue())

    return app


def _png_response(png_bytes: bytes):
    resp = make_response(png_bytes)
    resp.headers["Content-Type"] = "image/png"
    return resp


def _build_dtcore(g) -> dict:
    """Assemble the render_tone dtcore dict from a flat getter ``g(key, default)``
    (works over a request body, an edit_mono blob, or a config-backed getter)."""
    return {
        "sigmoid": {"enabled": bool(g("sig_enabled", True)), "contrast": float(g("sig_contrast", 0.735)),
                    "skew": float(g("sig_skew", 1.0)), "white": float(g("sig_white", 100.0)),
                    "black": float(g("sig_black", 0.7634))},
        "local_contrast": {"enabled": bool(g("lc_enabled", True)), "detail": float(g("lc_detail", 1.39)),
                    "highlights": float(g("lc_highlights", 0.5)), "shadows": float(g("lc_shadows", 0.5)),
                    "midtone": float(g("lc_midtone", 0.5))},
        "basic": {"enabled": bool(g("basic_enabled", True)), "exposure": float(g("basic_exposure", 0.0)),
                    "contrast": float(g("basic_contrast", 0.0)), "highlights": float(g("basic_highlights", 0.0)),
                    "shadows": float(g("basic_shadows", 0.0)), "whites": float(g("basic_whites", 0.0)),
                    "blacks": float(g("basic_blacks", 0.0)), "clahe": float(g("basic_clahe", 0.0))},
    }


#: Fixed B&W Contrast preset — the dtcore defaults ARE the B&W Contrast values.
BW_CONTRAST_DTCORE = _build_dtcore(lambda k, d: d)


def _crop_kwargs_from_edit(edit_crop) -> dict:
    """Unpack an edit_crop dict ({rotation_quarters, rect{x,y,w,h}}) into
    render_mono_bin's rotation_quarters/crop_rect kwargs. Empty dict for no crop."""
    if not edit_crop:
        return {}
    out = {}
    rq = edit_crop.get("rotation_quarters")
    if rq:
        out["rotation_quarters"] = int(rq)
    rect = edit_crop.get("rect")
    if rect:
        try:
            out["crop_rect"] = (float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def _mono_crop_for(rec, frame_portrait: bool) -> dict | None:
    """The crop the E1003 renders this image with, or None to render it as if unedited.

    Inheritance is unchanged — a forked ``edit_crop_mono`` wins, else the colour
    ``edit_crop`` — but each candidate only counts if it was authored FOR this mount's
    shape (see ``ImageRecord.crop_for``). A crop made for the other orientation is skipped,
    so the mono panel falls back to the next candidate and finally to auto framing.
    """
    if rec is None:
        return None
    want = Orientation.PORTRAIT if frame_portrait else Orientation.LANDSCAPE
    for crop in (rec.edit_crop_mono, rec.edit_crop):
        if not crop:
            continue
        target = crop.get("target")
        if not target or Orientation(target) == want:
            return crop
    return None


def _mono_content_portrait(config, rec, frame_portrait=False) -> bool:
    """Whether the mono render for this image composed PORTRAIT content (rotated sideways
    into the landscape wire buffer) — so a UI preview must un-rotate it to upright. Mirrors
    the render's decision using the SAME inputs _effective_mono_kwargs feeds render_mono_bin:
    the mono crop (if any) is applied first, then content is composed upright for the frame's
    orientation."""
    w = (rec.image_width or 0) if rec else 0
    h = (rec.image_height or 0) if rec else 0
    crop_kw = _crop_kwargs_from_edit(_mono_crop_for(rec, frame_portrait))
    return mono_e1003._mono_content_portrait(
        w, h,
        crop_kw.get("rotation_quarters", 0),
        crop_kw.get("crop_rect"),
        bool(frame_portrait),
    )


def _face_anchor_for(state, rec):
    """Face bboxes to aim a cover-crop at, or None when face-aware cropping is off /
    nothing was detected. Read from the classifier's observations (the same source the
    detail view uses), so it is independent of the CLAHE keep-out toggle."""
    if not getattr(state.config, "classifier_face_aware_crop_enabled", False):
        return None
    if rec is None or not getattr(rec, "sha1", None):
        return None
    obs = state.classifier.observations_for(rec.sha1)
    return (obs.face_bboxes or None) if obs else None


def _effective_mono_kwargs(config, rec, frame_portrait=False, crop_anchor_bboxes_norm=None) -> dict:
    """render_mono_bin kwargs for an image on an E1003 panel: the panel-wide
    ``mono_e1003_*`` default, overlaid with the image's per-image ``edit_mono``
    override. Branches on the tone profile — Faithful is the frozen legacy ramp
    path; B&W Contrast / Custom use the dtcore engine. Shared by the frame serve
    path + the mono dithered/thumbnail preview so both show the same render.

    Crop (amendment 5 — mono inherits the colour crop): a forked per-image ``edit_crop_mono``
    wins; otherwise the mono panel FOLLOWS the colour ``edit_crop`` (the editor's "Following
    Spectra crop" now actually follows). Each only applies on a mount of the shape it was
    authored for; otherwise the framing is governed by ``frame_portrait`` (the E1003 mount)
    + ``crop_to_fill_threshold`` — all via the shared ``frame_decision`` so mono and colour
    agree. Inheritance is safe now that both pipelines share that decision (the earlier
    regression was divergent crop policy)."""
    edit = rec.edit_mono if (rec and rec.edit_mono) else {}
    g = edit.get
    profile = g("profile", config.mono_e1003_profile)
    sharp = dict(
        sharpen_radius=g("sharpen_radius", config.mono_e1003_sharpen_radius),
        sharpen_percent=g("sharpen_amount", config.mono_e1003_sharpen_amount),
        sharpen_threshold=g("sharpen_threshold", config.mono_e1003_sharpen_threshold),
    )
    # forked mono crop wins; else the colour crop; either only if it was authored for
    # THIS mount's shape; else no crop (auto framing) — see _mono_crop_for.
    crop_kw = _crop_kwargs_from_edit(_mono_crop_for(rec, frame_portrait))
    frame_kw = dict(
        frame_portrait=bool(frame_portrait),
        # the single fill knob shared with colour (frame_decision) — it governs every
        # photo, cropped or not.
        crop_to_fill_threshold=float(getattr(config, "crop_to_fill_threshold", 0.0)),
        # aim a fill at the faces, same policy as the colour path
        crop_anchor_bboxes_norm=crop_anchor_bboxes_norm,
    )
    if profile == "bw_contrast":
        return dict(**sharp, **crop_kw, **frame_kw, dtcore=BW_CONTRAST_DTCORE)
    if profile == "custom":
        cg = lambda k, d: edit.get(k, getattr(config, "mono_e1003_" + k, d))
        return dict(**sharp, **crop_kw, **frame_kw, dtcore=_build_dtcore(cg))
    # faithful (default): the frozen legacy ramp pipeline
    return dict(
        **sharp,
        **crop_kw,
        **frame_kw,
        use_measured_ramp=g("use_measured_ramp", config.mono_e1003_use_measured_ramp),
        darken_gamma=g("darken_gamma", config.mono_e1003_darken_gamma),
        shadow_lift=g("shadow_lift", config.mono_e1003_shadow_lift),
        black_point=g("black_point", config.mono_e1003_black_point),
        white_point=g("white_point", config.mono_e1003_white_point),
        clarity=g("clarity", config.mono_e1003_clarity),
        contrast=g("contrast", config.mono_e1003_contrast),
        midtone=g("midtone", config.mono_e1003_midtone),
        highlights=g("highlights", config.mono_e1003_highlights),
        shadows=g("shadows", config.mono_e1003_shadows),
    )
