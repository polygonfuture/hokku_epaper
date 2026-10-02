"""ServeScheduler: rotation + serve stats + screen telemetry on top of ImageManager.

One DB file (``serve_scheduler.json``) carries all of:
- ``by_name``: per-image rotation pointer + cumulative stats
- ``last_served``: which image was served last (used for time-shown attribution)
- ``screens``: per-screen telemetry (request count, battery, frame state)
- ``next_for``: pre-computed next image per orientation (LANDSCAPE, PORTRAIT, NEUTRAL)
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from hokku_server.filesystem import (
    StateLoadError,
    atomic_write_json,
    backups_dir_for,
    load_json_guarded,
    snapshot,
)
from hokku_server.image_manager_abstract import AbstractImageManager
from hokku_server.image_record import ConvertStatus, ImageRecord
from hokku_server.orientation import Orientation
from hokku_server.screen_config import ScreenConfig
from hokku_server.screen_headers import battery_percent, parse_battery_header

logger = logging.getLogger(__name__)


_DB_FILENAME = "serve_scheduler.json"
# Dated snapshots, deduplicated by content — see Plans/CRASH_SAFE_STATE_PLAN.md
_BACKUP_KEEP = 14


@dataclass(frozen=True)
class ServeStats:
    show_index: int
    last_served_at: float | None
    total_show_count: int
    total_show_minutes: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ServeStats:
        return cls(
            show_index=int(d.get("show_index", 0)),
            last_served_at=d.get("last_served_at"),
            total_show_count=int(d.get("total_show_count", 0)),
            total_show_minutes=float(d.get("total_show_minutes", 0.0)),
        )


@dataclass(frozen=True)
class ScreenTelemetryEntry:
    ip: str
    request_count: int
    last_seen_at: float
    last_sleep_seconds: int | None
    last_served: str | None
    battery_mv: int | None
    battery_percent: int | None
    battery_seen_at: float | None
    frame_state: dict | None
    last_log: str
    last_log_at: float
    #: Panel type the frame announced via X-Panel-Type (e.g. "mono16_e1003").
    #: None = a classic Spectra frame that sends no panel-type header. Lets the
    #: UI capability-gate mono-panel controls to frames that actually have one.
    panel_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ScreenTelemetryEntry:
        return cls(
            ip=d.get("ip", ""),
            request_count=int(d.get("request_count", 0)),
            last_seen_at=float(d.get("last_seen_at", 0.0)),
            last_sleep_seconds=d.get("last_sleep_seconds"),
            last_served=d.get("last_served"),
            battery_mv=d.get("battery_mv"),
            battery_percent=d.get("battery_percent"),
            battery_seen_at=d.get("battery_seen_at"),
            frame_state=d.get("frame_state"),
            last_log=d.get("last_log", ""),
            last_log_at=float(d.get("last_log_at", 0.0)),
            panel_type=d.get("panel_type"),
        )


class ServeScheduler:
    """Fair-rotation scheduler + screen telemetry collector."""

    def __init__(
        self, manager: AbstractImageManager, rng: random.Random | None = None
    ) -> None:
        self._manager = manager
        self._db_path = Path(manager.config.cache_dir) / _DB_FILENAME
        self._lock = threading.RLock()
        self._stats: dict[str, ServeStats] = {}
        self._screens: dict[str, ScreenTelemetryEntry] = {}
        self._screen_configs: dict[str, ScreenConfig] = {}
        self._last_served: tuple[str, float] | None = None
        self._next_for: dict[Orientation, str | None] = dict.fromkeys(Orientation, None)
        # Per-screen forced next image (screen name -> image name). Persisted:
        # frames sleep for hours, so an override must survive a server restart.
        self._next_for_screen: dict[str, str] = {}
        # Per-screen COMMITTED next image (screen name -> image name): the resolved
        # rotation pick with cross-screen de-dup + random tiebreak ALREADY applied at
        # commit time. This is the single serve authority — pick_next(screen) returns it
        # verbatim and the frames-drawer reads the same value, so "up next" == what serves,
        # always. Recomputed for ALL screens on every advance / pin change / reconcile.
        # Persisted (frames sleep for hours). A pin (_next_for_screen) still wins over this.
        self._committed_next_for_screen: dict[str, str] = {}
        # Per-frame HARD-skip "penalty box": screen -> {image -> release_serve_count}. A skipped
        # image is excluded from THAT screen's up-next until the screen's serve count passes the
        # release count (~a full cycle). Per-frame, NOT global — the same photo can still be up
        # next on another frame. Persisted so a skip survives a restart while the frame sleeps.
        self._skip_until: dict[str, dict[str, int]] = {}
        # Per-screen count of actual serves (advances the skip cooldown clock). Persisted.
        self._serve_count: dict[str, int] = {}
        # Rotation picks the LEAST-shown image, breaking ties at RANDOM (not alphabetically) so
        # a fresh library — where most images are tied at show_index 0 — doesn't march through in
        # filename order. Injectable + seedable for deterministic tests; defaults to system entropy.
        self._rng = rng if rng is not None else random.Random()
        self._load()
        # Pre-determine the next image right now so the UI can show it
        # immediately without waiting for the first screen request.
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            if ready:
                ready_names = {r.name for r in ready}
                self._reconcile(ready_names)
                self._precompute_all_locked(ready)
                self._recommit_all_screens_locked(ready)

    def _least_shown_random(self, names: list[str]) -> str | None:
        """Pick a name with the minimum show_index, breaking ties uniformly at random.

        Fairness is unchanged (still least-shown-first, so every image is shown once before any
        repeats); only the WITHIN-tier order is randomised, which adds spontaneity. Returns None
        for an empty list. Must be called under self._lock (reads self._stats)."""
        if not names:
            return None
        min_idx = min(self._stats[n].show_index for n in names)
        tied = [n for n in names if self._stats[n].show_index == min_idx]
        return self._rng.choice(tied)

    # ── Rotation ─────────────────────────────────────────────────

    def pick_next(self, orientation: Orientation, screen_name: str | None = None) -> str | None:
        """Return the pre-determined next image for the given orientation filter.

        orientation=NEUTRAL means no filter — returns the global best next image.
        Reconciles state with manager.list() before returning — adds new
        entries, drops orphans, resets show_index for everyone when a new
        image appears so it gets a fair chance immediately.

        ``screen_name`` (a real frame check-in) returns that screen's COMMITTED next
        image — the rotation pick with cross-screen de-dup + random tiebreak already
        resolved at commit time (see _recommit_all_screens_locked). This is the exact
        value the frames-drawer shows, so "up next" == what serves, always.
        ``screen_name=None`` (UI preview / no frame context) keeps the original
        orientation-keyed shared-pick behaviour exactly.
        """
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)

            if not ready:
                self._next_for = dict.fromkeys(Orientation, None)
                self._committed_next_for_screen.clear()
                self._save()
                return None

            # Real frame: serve the committed per-screen pick (the drawer reads the same
            # value). Recommit if it's stale/absent so the served image is always valid.
            if screen_name is not None:
                committed = self._committed_next_for_screen.get(screen_name)
                if committed not in ready_names:
                    # Absent/stale — recommit, ensuring THIS frame is in the commit universe
                    # even if it hasn't recorded telemetry yet (first check-in).
                    self._recommit_all_screens_locked(ready, include_screen=screen_name)
                    self._save()
                    committed = self._committed_next_for_screen.get(screen_name)
                return committed

            # No frame context (UI preview): honour the precomputed orientation slot.
            if self._next_for.get(orientation) in ready_names:
                return self._next_for[orientation]

            # Pre-computed choice is stale or absent — recompute all orientations.
            self._precompute_all_locked(ready)
            self._save()
            return self._next_for.get(orientation)

    def _recommit_all_screens_locked(
        self,
        ready: list[ImageRecord],
        include_screen: str | None = None,
        just_served: tuple[str, str] | None = None,
    ) -> None:
        """Commit a de-duplicated next image for EVERY registered screen at once.

        Committed picks across screens are kept DISTINCT (no two frames serve the same photo
        at once) and are the least-shown eligible images (fair rotation), with random
        tiebreak. A frame never re-commits the image it just served (``just_served``).

        MINIMAL DISTURBANCE (the drawer==serve guarantee across frames): a screen KEEPS its
        existing valid committed pick on EVERY recommit — passive (reload/poll/new-screen) AND
        on another frame's advance. Only the frame that just advanced (``just_served``) is
        repicked; every OTHER screen holds the exact image its drawer is showing, so serving
        one frame never silently changes what another frame will serve. A held pick is dropped
        only if it became invalid (image gone / no longer eligible / now displayed or committed
        elsewhere — a real collision); such a screen is then rematched. Frames still sleep and
        wake seconds apart, so this is what makes "up next" match the actual serve every time.
        Pins always win. Fairness is preserved: each frame repicks least-shown when IT advances,
        and in a comfortably-sized library idle holds are negligible (a tight library forces
        collision-driven rematches anyway).

        ``include_screen`` forces a screen into the universe even with no telemetry/config/pin
        yet (first check-in). Must be called under self._lock."""
        ready_names = {r.name for r in ready}
        screen_names = (
            set(self._screens)
            | set(self._screen_configs)
            | set(self._next_for_screen)
        )
        if include_screen is not None:
            screen_names.add(include_screen)

        def _eligible_for(sname: str) -> list[ImageRecord]:
            cfg = self.get_screen_config(sname)
            orient = cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL
            if orient == Orientation.NEUTRAL:
                return ready
            return [r for r in ready if r.matches_orientation_filter(orient)]

        # Images each screen is currently displaying (its last_served). ``just_served`` is what
        # the advancing frame served THIS round (telemetry lags a step), so it counts as that
        # frame's current display — used to keep it from immediately repeating itself.
        displaying = {
            sname: e.last_served
            for sname, e in self._screens.items()
            if e.last_served
        }
        if just_served is not None:
            displaying[just_served[0]] = just_served[1]

        committed: dict[str, str] = {}
        in_use: set[str] = set()
        # The one screen that just advanced (consumed its committed pick) MUST repick; every
        # other screen keeps its pick so serving one frame never reshuffles another's drawer.
        advancer = just_served[0] if just_served is not None else None

        # Pass 1 — pins always win.
        for sname in sorted(screen_names):
            pin = self._next_for_screen.get(sname)
            if pin in ready_names:
                committed[sname] = pin
                in_use.add(pin)

        # Pass 2 — PRESERVE each screen's still-valid committed pick (except the advancer), on
        # every recommit. A pick is kept only if it's still ready, still eligible, not already
        # taken by a pin/another kept pick, and not currently displayed by ANOTHER screen (that
        # would be a dup-on-wall). Screens whose pick is dropped are rematched fresh in Pass 3.
        for sname in sorted(screen_names):
            if sname in committed or sname == advancer:
                continue
            prev = self._committed_next_for_screen.get(sname)
            others_display = {img for o, img in displaying.items() if o != sname}
            if (
                prev in ready_names
                and prev not in in_use
                and prev not in others_display
                and prev not in self._active_skips_for(sname)
                and prev in {r.name for r in _eligible_for(sname)}
            ):
                committed[sname] = prev
                in_use.add(prev)

        # Pass 3 — assign a DISTINCT least-shown image to every screen still needing one, as a
        # bipartite MATCHING (screens ↔ images) rather than a greedy per-screen grab.
        #
        # Why matching, not greedy: with several screens and mixed orientation filters a greedy
        # "each screen takes its own least-shown" both STARVES (low-named screens hog the fresh
        # images, spread hit 21 across 8 frames) and DUPLICATES (a square, eligible to every
        # filter, got committed to two frames while other images sat unused). An augmenting-path
        # matching maximises the number of DISTINCT assignments, so two screens share an image
        # only when there are genuinely fewer distinct eligible images than screens — the true
        # unavoidable case. Preferring least-shown images inside the match keeps rotation fair.
        #
        # NO-DUPLICATE (hard): the matched image is distinct across screens AND excludes images
        # OTHER screens currently display (``_avail_for`` removes them), so nothing already on a
        # wall is handed to a second frame. NO-REPEAT (soft): a first matching pass forbids each
        # screen's own last-served; screens left unmatched retry with that rule dropped, so a
        # repeat happens only when unavoidable. Determinism/reload: candidate images are ordered
        # by (show_index, seeded-RNG), and screens are processed most-shown-display first, so a
        # reload reproduces the same assignment from persisted state.
        already = set(committed.values())  # pins + preserved picks — images the match must avoid

        def _avail_for(sname: str) -> list[str]:
            others_display = {img for o, img in displaying.items() if o != sname}
            skipped = self._active_skips_for(sname)   # per-frame penalty box (this screen only)
            return [
                r.name for r in _eligible_for(sname)
                if r.name not in already and r.name not in others_display
                and r.name not in skipped
            ]

        def _img_sort_key(name: str) -> tuple:
            idx = self._stats[name].show_index if name in self._stats else 0
            return (idx, self._rng.random())  # least-shown first; seeded-RNG tiebreak

        def _screen_priority(sname: str) -> tuple:
            shown = displaying.get(sname)
            shown_idx = self._stats[shown].show_index if shown in self._stats else -1
            return (-shown_idx, self._rng.random())  # most-shown display moves first

        need = sorted(
            (s for s in screen_names if s not in committed), key=_screen_priority
        )
        match_frame: dict[str, str] = {}   # screen -> image
        match_img: dict[str, str] = {}     # image -> screen

        def _augment(sname: str, seen: set[str], forbid_own: bool) -> bool:
            own = displaying.get(sname)
            cands = sorted(_avail_for(sname), key=_img_sort_key)
            for img in cands:
                if forbid_own and img == own:
                    continue
                if img in seen:
                    continue
                seen.add(img)
                if img not in match_img or _augment(match_img[img], seen, forbid_own):
                    match_img[img] = sname
                    match_frame[sname] = img
                    return True
            return False

        # First pass forbids own-last (no back-to-back repeat); leftovers retry allowing it,
        # then finally accept a shared image only if the screen has literally no distinct option.
        for sname in need:
            if not _augment(sname, set(), forbid_own=True):
                _augment(sname, set(), forbid_own=False)
        for sname in need:
            if sname not in match_frame:
                # No distinct image available (fewer eligible images than screens): fall back to
                # the least-shown eligible image, accepting an unavoidable duplicate/repeat.
                eligible = [r.name for r in _eligible_for(sname)]
                pick = self._least_shown_random(eligible)
                if pick is not None:
                    match_frame[sname] = pick

        committed.update(match_frame)
        self._committed_next_for_screen = committed

    def _recommit_screens_from_manager_locked(self) -> None:
        """Reconcile against the manager and recommit every screen. Convenience wrapper for
        the pin/config mutation paths (which don't already hold a ``ready`` list). Must be
        called under self._lock."""
        ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
        self._reconcile({r.name for r in ready})
        self._recommit_all_screens_locked(ready)

    def committed_next_for_screen(self, screen_name: str) -> str | None:
        """The image this screen will serve NEXT: its pin if pending-and-ready, otherwise its
        committed rotation pick. This is exactly what the next serve returns, so the drawer
        reads this value to guarantee "up next" == what serves. Read-only (no recompute)."""
        with self._lock:
            pin = self._next_for_screen.get(screen_name)
            if pin is not None:
                rec = self._manager.status(pin)
                if rec is not None and rec.convert_status == ConvertStatus.OK:
                    return pin
            return self._committed_next_for_screen.get(screen_name)

    def committed_snapshot(self) -> dict[str, str | None]:
        """Every known screen's committed 'next' (pin-or-rotation), as ONE atomic read under the
        lock. Used by the serve-decision log to make cross-frame reshuffle visible: compare the
        snapshot before a serve to the one after and any OTHER screen whose value changed was
        mutated by this screen's advance."""
        with self._lock:
            names = (
                set(self._screens)
                | set(self._screen_configs)
                | set(self._next_for_screen)
                | set(self._committed_next_for_screen)
            )
            out: dict[str, str | None] = {}
            for sname in sorted(names):
                pin = self._next_for_screen.get(sname)
                if pin is not None:
                    rec = self._manager.status(pin)
                    if rec is not None and rec.convert_status == ConvertStatus.OK:
                        out[sname] = pin
                        continue
                out[sname] = self._committed_next_for_screen.get(sname)
            return out

    def library_changed(self) -> None:
        """Reconcile + recommit after the image library changed OUT OF BAND (e.g. an image was
        deleted via the API, rather than consumed by a serve). Drops any committed pick, pin, or
        rotation-stat whose image is now gone, and re-picks ONLY the screens that lost theirs —
        this is a passive recommit, so a frame whose "up next" is unaffected keeps its exact
        pick. Without this, a deleted up-next image lingers in the drawer (a dead thumbnail →
        black) until the next serve happens to reconcile. Safe to call on any delete."""
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)
            if not ready:
                self._next_for = dict.fromkeys(Orientation, None)
                self._committed_next_for_screen.clear()
                self._save()
                return
            self._precompute_all_locked(ready)
            self._recommit_all_screens_locked(ready)
            self._save()

    def skip_next(self, screen_name: str) -> str | None:
        """Skip this screen's current 'up next' — pass on it THIS round, then put it straight
        back in the pool. Re-picks a fresh up-next for this screen only.

        The skipped photo is excluded from just the next pick, so the skip visibly does
        something, and is eligible again as soon as this frame has served the replacement.
        It keeps its place in the rotation: skipping is "not this one, now", not a penalty.

        (It used to be boxed for a FULL CYCLE — ``cooldown`` = eligible pool − 1. That scales
        with the library, so at 184 eligible photos and two refreshes a day one skip removed a
        photo from that frame for ~3 months, and got worse as the library grew. Nothing about
        tapping Skip implies that.)

        Per-frame, not global: tracked in a per-screen box, NOT the shared show count, so the
        same photo can still be up next on a different frame. Other frames' committed picks are
        untouched (passive recommit). Returns the new committed image — or the same one when the
        screen has no other eligible photo. A skipped pin is cleared. Not a real display:
        rotation stats (total_show_count/minutes) are untouched, so global fairness is
        undisturbed. For a permanent removal, Delete is the tool."""
        with self._lock:
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)
            if not ready:
                return None

            # The current up-next is the pin (if still ready) else the committed rotation pick.
            current = self._next_for_screen.get(screen_name)
            if current not in ready_names:
                current = self._committed_next_for_screen.get(screen_name)

            # This screen's eligible pool (its orientation filter).
            cfg = self.get_screen_config(screen_name)
            orient = cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL
            if orient == Orientation.NEUTRAL:
                eligible = [r.name for r in ready]
            else:
                eligible = [r.name for r in ready if r.matches_orientation_filter(orient)]
            n_elig = len(set(eligible))

            # Only skip when there's genuinely something else to move to.
            if current is not None and current in eligible and n_elig > 1:
                # Hold it out of the NEXT pick only — enough to guarantee the skip changes what
                # you see — then it is back in the pool with its place in the rotation intact.
                cooldown = 1
                box = self._skip_until.setdefault(screen_name, {})
                box[current] = self._serve_count.get(screen_name, 0) + cooldown
                if self._next_for_screen.get(screen_name) == current:
                    del self._next_for_screen[screen_name]     # a skipped pin is cleared
                self._committed_next_for_screen.pop(screen_name, None)   # force a fresh pick

            # Re-pick this screen (others preserved — passive, minimal-disturbance recommit).
            self._precompute_all_locked(ready)
            self._recommit_all_screens_locked(ready, include_screen=screen_name)
            self._save()
            return self._committed_next_for_screen.get(screen_name)

    def _active_skips_for(self, screen_name: str) -> set[str]:
        """Images currently boxed (skipped) for a screen — those whose cooldown hasn't elapsed
        (release count is still ahead of the screen's serve count). Must hold self._lock."""
        served = self._serve_count.get(screen_name, 0)
        return {
            img for img, release_at in self._skip_until.get(screen_name, {}).items()
            if release_at > served
        }

    def mark_served(
        self, name: str, screen_name: str | None = None, *, count_turn: bool = True
    ) -> None:
        """Bump rotation pointer and stats. Attributes elapsed time to the
        previously-served image. Pre-computes the next image for all orientations
        so the UI reflects the upcoming choice immediately.

        When ``screen_name`` is given and that screen had a pending per-screen
        override for exactly this image, the override is consumed.

        ``count_turn=False`` serves the image WITHOUT spending its turn: no show_index,
        no show count, no last-served stamp, no time attributed to the previous image.
        Debug fast-refresh passes this. A 3-minute debug cycle across two frames burns 40
        turns an hour — far more than a month of real refreshes — so counting them let a
        few hours of firmware testing mark the whole library "shown" and demote it out of
        the priority tier, while nothing had actually been on the wall for more than a
        debug interval. Screen bookkeeping below still runs: the frame really is showing
        this image, it just doesn't owe the rotation anything for it."""
        with self._lock:
            now = time.time()
            if count_turn:
                self._attribute_show_time(now)

                cur = self._stats.get(name) or ServeStats(0, None, 0, 0.0)
                self._stats[name] = ServeStats(
                    show_index=cur.show_index + 1,
                    last_served_at=now,
                    total_show_count=cur.total_show_count + 1,
                    total_show_minutes=cur.total_show_minutes,
                )
                self._last_served = (name, now)
            else:
                # Don't let the NEXT real serve attribute this debug interval as display
                # time on the previous image either.
                self._last_served = None
            if screen_name is not None:
                # Advance this screen's serve clock (releases expired per-frame skips) and prune
                # any now-elapsed skip entries so the box doesn't grow without bound.
                self._serve_count[screen_name] = self._serve_count.get(screen_name, 0) + 1
                box = self._skip_until.get(screen_name)
                if box:
                    served = self._serve_count[screen_name]
                    for img in [i for i, rel in box.items() if rel <= served]:
                        del box[img]
                    if not box:
                        self._skip_until.pop(screen_name, None)
            if screen_name is not None and self._next_for_screen.get(screen_name) == name:
                del self._next_for_screen[screen_name]
            # This screen just consumed its committed pick — clear it so the recommit's
            # stability pass gives it a FRESH next (otherwise it would preserve the
            # just-served image and never advance).
            if screen_name is not None:
                self._committed_next_for_screen.pop(screen_name, None)
            # Consumed — recompute the next image for all orientations AND all screens
            # immediately, so every frame's committed pick (and its drawer) stays current
            # and collision-free the instant this frame advances.
            ready = [r for r in self._manager.list() if r.convert_status == ConvertStatus.OK]
            ready_names = {r.name for r in ready}
            self._reconcile(ready_names)
            self._precompute_all_locked(ready)
            self._recommit_all_screens_locked(
                ready,
                just_served=(screen_name, name) if screen_name is not None else None,
            )
            self._save()

    # ── Stats retrieval ──────────────────────────────────────────

    def stats(self) -> dict[str, ServeStats]:
        with self._lock:
            return dict(self._stats)

    def stats_for(self, name: str) -> ServeStats | None:
        with self._lock:
            return self._stats.get(name)

    def last_served(self) -> tuple[str, float] | None:
        with self._lock:
            return self._last_served

    def peek_next(self, orientation: Orientation) -> str | None:
        """Return the pre-determined next image for the given orientation without consuming it."""
        with self._lock:
            return self._next_for.get(orientation)

    def set_next(self, name: str) -> None:
        """Force a specific image to be served next (overrides rotation order).

        Raises ValueError if the image is not currently ready to serve.
        """
        with self._lock:
            ready = {r.name for r in self._manager.list() if r.convert_status == ConvertStatus.OK}
            if name not in ready:
                raise ValueError(f"Image {name!r} is not ready to serve")
            # Override all orientation slots to the forced image (it will be
            # filtered by orientation at serve time if a filter is active).
            for o in Orientation:
                self._next_for[o] = name
            # Real frames serve their COMMITTED per-screen pick, not _next_for, so a global
            # "show next" must pin every known screen to take effect on the frames themselves.
            for sname in (set(self._screens) | set(self._screen_configs)):
                self._next_for_screen[sname] = name
            self._recommit_screens_from_manager_locked()
            self._save()

    # ── Per-screen forced next ───────────────────────────────────

    def set_next_for_screen(self, screen_name: str, image_name: str) -> None:
        """Force a specific image to be served next to ONE screen.

        Bypasses the screen's orientation filter (both orientations are always
        rendered for OK images, so a cached binary exists either way).
        Raises ValueError if the image is not currently ready to serve.
        """
        with self._lock:
            ready = {r.name for r in self._manager.list() if r.convert_status == ConvertStatus.OK}
            if image_name not in ready:
                raise ValueError(f"Image {image_name!r} is not ready to serve")
            self._next_for_screen[screen_name] = image_name
            self._recommit_screens_from_manager_locked()
            self._save()

    def clear_next_for_screen(self, screen_name: str) -> None:
        """Cancel a pending per-screen override. Idempotent."""
        with self._lock:
            if self._next_for_screen.pop(screen_name, None) is not None:
                self._recommit_screens_from_manager_locked()
                self._save()

    def peek_next_for_screen(self, screen_name: str) -> str | None:
        """Return the screen's pending override iff it is ready to serve NOW.

        A temporarily not-ready image (e.g. mid-reconversion after a cache clear)
        returns None but the override is RETAINED; it is dropped only when the
        image is deleted (see _reconcile) or after it has been served.
        """
        with self._lock:
            name = self._next_for_screen.get(screen_name)
            if name is None:
                return None
            rec = self._manager.status(name)
            if rec is None or rec.convert_status != ConvertStatus.OK:
                return None
            return name

    # ── Screen telemetry ─────────────────────────────────────────

    def record_screen_call(
        self,
        screen_name: str,
        screen_ip: str,
        sleep_seconds: int,
        served_name: str | None,
        battery_mv: int | None,
        frame_state: dict | None,
        log: str | None = None,
        panel_type: str | None = None,
    ) -> None:
        with self._lock:
            now = time.time()
            existing = self._screens.get(screen_name)
            req_count = (existing.request_count + 1) if existing else 1
            # Sticky: keep the last announced panel type if this call omits it.
            panel_type = panel_type or (existing.panel_type if existing else None)

            # Frame-state may carry a more reliable battery reading.
            if frame_state and isinstance(frame_state.get("bat_mv"), (int, float)):
                fs_mv = parse_battery_header(str(int(frame_state["bat_mv"])))
                if fs_mv is not None:
                    battery_mv = fs_mv

            bat_pct = None
            bat_mv_value = existing.battery_mv if existing else None
            bat_seen = existing.battery_seen_at if existing else None
            if battery_mv is not None and battery_mv > 0:
                bat_mv_value = int(battery_mv)
                bat_pct = battery_percent(battery_mv)
                bat_seen = now
            elif existing:
                bat_pct = existing.battery_percent

            fs_with_meta = None
            if frame_state:
                fs_with_meta = dict(frame_state)
                clk_now = frame_state.get("clk_now")
                if isinstance(clk_now, (int, float)) and clk_now > 0:
                    fs_with_meta["clk_drift_s"] = int(clk_now - now)
                fs_with_meta["seen_at"] = now

            last_log = existing.last_log if existing else ""
            last_log_at = existing.last_log_at if existing else 0.0
            if log:
                last_log = log
                last_log_at = now

            self._screens[screen_name] = ScreenTelemetryEntry(
                ip=screen_ip,
                request_count=req_count,
                last_seen_at=now,
                last_sleep_seconds=int(sleep_seconds),
                last_served=served_name
                if served_name is not None
                else (existing.last_served if existing else None),
                battery_mv=bat_mv_value,
                battery_percent=bat_pct,
                battery_seen_at=bat_seen,
                frame_state=fs_with_meta
                if fs_with_meta is not None
                else (existing.frame_state if existing else None),
                last_log=last_log,
                last_log_at=last_log_at,
                panel_type=panel_type,
            )
            # A newly-seen screen has no committed pick yet — commit one now so its drawer
            # populates immediately (and matches what it will serve). Skipped on every later
            # poll (recommits happen on advance / pin / config changes).
            if screen_name not in self._committed_next_for_screen:
                self._recommit_screens_from_manager_locked()
            self._save()

    def screens(self) -> dict[str, ScreenTelemetryEntry]:
        with self._lock:
            return dict(self._screens)

    def remove_screen(self, name: str) -> None:
        """Remove a screen's telemetry, serve-stats, and config records.

        Idempotent — silently does nothing if the name is not known.
        The screen can re-register itself the next time it connects.
        """
        with self._lock:
            self._screens.pop(name, None)
            self._stats.pop(name, None)
            self._screen_configs.pop(name, None)
            self._next_for_screen.pop(name, None)
            self._committed_next_for_screen.pop(name, None)
            self._skip_until.pop(name, None)
            self._serve_count.pop(name, None)
            if self._last_served and self._last_served[0] == name:
                self._last_served = None
            self._recommit_screens_from_manager_locked()
            self._save()

    # ── Per-screen config ─────────────────────────────────────────

    def get_screen_config(self, name: str) -> ScreenConfig:
        """Return the full config for a screen (default ScreenConfig if not set)."""
        with self._lock:
            return self._screen_configs.get(name, ScreenConfig())

    def set_screen_config(self, name: str, config: ScreenConfig) -> None:
        """Persist the full config for a screen."""
        with self._lock:
            self._screen_configs[name] = config
            # Orientation filter may have changed — recommit so this screen's next pick
            # matches its new filter (and de-dup against others still holds).
            self._recommit_screens_from_manager_locked()
            self._save()

    # ── Internals ────────────────────────────────────────────────

    def _atomic_write_json(self, payload: dict) -> None:
        atomic_write_json(self._db_path, payload)

    def _precompute_all_locked(self, ready: list[ImageRecord]) -> None:
        """Pre-compute the next image for every orientation value.

        NEUTRAL = unfiltered (best across all images).
        LANDSCAPE/PORTRAIT = best among images matching that orientation filter.
        Must be called under self._lock.
        """
        for orientation in Orientation:
            if orientation == Orientation.NEUTRAL:
                eligible = ready
            else:
                eligible = [r for r in ready if r.matches_orientation_filter(orientation)]
            self._next_for[orientation] = self._least_shown_random(
                [r.name for r in eligible]
            )

    def _reconcile(self, ready_names: set[str]) -> None:
        all_names = {r.name for r in self._manager.list()}
        # Drop orphans.
        for name in list(self._stats.keys()):
            if name not in ready_names and name not in all_names:
                del self._stats[name]
        # Drop per-screen overrides whose image was DELETED (a merely not-ready
        # image keeps its override — see peek_next_for_screen).
        for sname in list(self._next_for_screen.keys()):
            if self._next_for_screen[sname] not in all_names:
                del self._next_for_screen[sname]
        # Drop committed picks whose image was DELETED (recommit re-fills them; a merely
        # not-ready image is left for pick_next to refresh on demand).
        for sname in list(self._committed_next_for_screen.keys()):
            if self._committed_next_for_screen[sname] not in all_names:
                del self._committed_next_for_screen[sname]
        # Drop per-frame skip entries whose image was DELETED (no point boxing a gone photo).
        for sname in list(self._skip_until.keys()):
            box = self._skip_until[sname]
            for img in [i for i in box if i not in all_names]:
                del box[img]
            if not box:
                del self._skip_until[sname]

        # Add fresh entries. A new image starts at the CURRENT minimum show_index across the
        # library (0 if empty) — so it joins the least-shown tier and gets a fair turn within
        # the next cycle, WITHOUT touching any existing image's history. (The old code reset
        # every nonzero index to 1 on any upload, which wiped the whole library's rotation
        # history — so frequent uploads kept flattening fairness back to near-flat. show_index
        # is a global counter but only ever compared within an orientation-filtered candidate
        # set at pick time, so a global min is safe: a new image can only affect the rotation
        # of frames whose filter it matches.)
        currently_known = set(self._stats.keys())
        truly_new = ready_names - currently_known
        if truly_new:
            start_index = min((s.show_index for s in self._stats.values()), default=0)
            for name in truly_new:
                self._stats[name] = ServeStats(start_index, None, 0, 0.0)

    def _attribute_show_time(self, now: float) -> None:
        if self._last_served is None:
            return
        prev_name, prev_time = self._last_served
        elapsed_min = (now - prev_time) / 60.0
        if not (0 < elapsed_min < 60 * 24 * 30):  # sanity bound
            return
        cur = self._stats.get(prev_name)
        if cur is None:
            return
        self._stats[prev_name] = replace(
            cur,
            total_show_minutes=cur.total_show_minutes + elapsed_min,
        )

    def _load(self) -> None:
        """Load rotation state, recovering from the .bak if the primary is unreadable.

        Unlike the image DB this state is DERIVED — losing it costs rotation history, not user
        work — so a total loss starts empty rather than refusing to boot. The .bak and the
        dated snapshots still make that a last resort instead of the first response.
        """
        try:
            data, source = load_json_guarded(self._db_path, label="rotation state")
        except StateLoadError as e:
            logger.error("%s — starting with empty rotation history", e)
            return
        if data is None:
            return
        for name, blob in data.get("by_name", {}).items():
            try:
                self._stats[name] = ServeStats.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed serve stats for %r: %s", name, e)
        for name, blob in data.get("screens", {}).items():
            try:
                self._screens[name] = ScreenTelemetryEntry.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed telemetry entry %r: %s", name, e)
        for name, blob in data.get("screen_configs", {}).items():
            try:
                self._screen_configs[name] = ScreenConfig.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed screen config for %r: %s", name, e)
        ls = data.get("last_served")
        if isinstance(ls, dict) and "name" in ls and "served_at" in ls:
            try:
                self._last_served = (ls["name"], float(ls["served_at"]))
            except (TypeError, ValueError):
                pass

        # Archive only what just parsed cleanly (never in-memory state), deduplicated by
        # content so quiet days cost nothing.
        snapshot(self._db_path, backups_dir_for(self._db_path.parent),
                 store="serve_scheduler", keep=_BACKUP_KEEP)
        if source == "backup":
            self._save()      # promote the recovered state back to primary
        # Load next_for; migrate from old "next_image" key if needed.
        next_for_raw = data.get("next_for")
        if isinstance(next_for_raw, dict):
            for o in Orientation:
                val = next_for_raw.get(o.value)
                self._next_for[o] = val if isinstance(val, str) else None
        else:
            old_next = data.get("next_image")
            if isinstance(old_next, str):
                self._next_for[Orientation.NEUTRAL] = old_next
        nfs = data.get("next_for_screen")
        if isinstance(nfs, dict):
            for sname, iname in nfs.items():
                if isinstance(sname, str) and isinstance(iname, str):
                    self._next_for_screen[sname] = iname
        cns = data.get("committed_next_for_screen")
        if isinstance(cns, dict):
            for sname, iname in cns.items():
                if isinstance(sname, str) and isinstance(iname, str):
                    self._committed_next_for_screen[sname] = iname
        su = data.get("skip_until")
        if isinstance(su, dict):
            for sname, box in su.items():
                if isinstance(sname, str) and isinstance(box, dict):
                    clean = {i: int(r) for i, r in box.items()
                             if isinstance(i, str) and isinstance(r, (int, float))}
                    if clean:
                        self._skip_until[sname] = clean
        svc = data.get("serve_count")
        if isinstance(svc, dict):
            for sname, n in svc.items():
                if isinstance(sname, str) and isinstance(n, (int, float)):
                    self._serve_count[sname] = int(n)

    def _save(self) -> None:
        payload = {
            "version": 1,
            "next_for": {o.value: self._next_for.get(o) for o in Orientation},
            "next_for_screen": dict(self._next_for_screen),
            "committed_next_for_screen": dict(self._committed_next_for_screen),
            "skip_until": {s: dict(box) for s, box in self._skip_until.items()},
            "serve_count": dict(self._serve_count),
            "last_served": (
                {"name": self._last_served[0], "served_at": self._last_served[1]}
                if self._last_served
                else None
            ),
            "by_name": {n: s.to_dict() for n, s in self._stats.items()},
            "screens": {n: t.to_dict() for n, t in self._screens.items()},
            "screen_configs": {n: c.to_dict() for n, c in self._screen_configs.items()},
        }
        self._atomic_write_json(payload)
