"""ServeScheduler: rotation fairness, stats, telemetry, persistence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hokku_server.app_config import AppConfig
from hokku_server.image_manager_single import SingleThreadedImageManager
from hokku_server.orientation import Orientation
from hokku_server.screen_config import ScreenConfig
from hokku_server.serve_scheduler import ServeScheduler


def _setup(
    app_config: AppConfig, make_test_image, names: list[str]
) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    upload = Path(app_config.upload_dir)
    for n in names:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def _setup_with_sizes(
    app_config: AppConfig,
    make_test_image,
    name_sizes: list[tuple[str, tuple[int, int]]],
) -> tuple[SingleThreadedImageManager, ServeScheduler]:
    """Like _setup but lets the caller specify per-image dimensions."""
    upload = Path(app_config.upload_dir)
    for name, size in name_sizes:
        make_test_image(upload / name, size=size)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr)


def test_pick_next_empty(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    assert sched.pick_next(Orientation.NEUTRAL) is None


def test_pick_next_single(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    assert sched.pick_next(Orientation.NEUTRAL) == "a.png"


def test_fair_rotation(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    counts = {"a.png": 0, "b.png": 0, "c.png": 0}
    for _ in range(9):
        n = sched.pick_next(Orientation.NEUTRAL)
        assert n is not None
        sched.mark_served(n)
        counts[n] += 1
    assert counts == {"a.png": 3, "b.png": 3, "c.png": 3}


def test_stats_after_serves(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    n = sched.pick_next(Orientation.NEUTRAL)
    assert n is not None
    sched.mark_served(n)
    s = sched.stats_for("a.png")
    assert s is not None
    assert s.total_show_count == 1
    assert s.last_served_at is not None


def test_debug_serve_does_not_spend_a_turn(app_config: AppConfig, make_test_image):
    """Debug fast-refresh must not touch the fairness rotation.

    It cycles every 3 minutes, so a few hours of firmware testing burns more turns than a
    month of real refreshes — which marked the whole library "shown" and demoted it out of
    the priority tier while nothing had actually been displayed for more than a debug
    interval. A debug serve leaves every rotation counter untouched.
    """
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])

    sched.mark_served("a.png", count_turn=False)
    s = sched.stats_for("a.png")
    assert s is not None
    assert s.show_index == 0 and s.total_show_count == 0 and s.last_served_at is None

    # ...so it is still the least-shown, and fairness is unchanged: a real serve after any
    # number of debug serves is the FIRST turn either photo has spent.
    for _ in range(5):
        sched.mark_served("a.png", count_turn=False)
    sched.mark_served("a.png")
    assert sched.stats_for("a.png").total_show_count == 1

    # and a debug serve doesn't back-fill display time onto the previous image either
    sched.mark_served("b.png", count_turn=False)
    sched.mark_served("b.png")
    assert sched.stats_for("a.png").total_show_minutes == 0.0


def test_persistence(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.mark_served("a.png")
    sched.mark_served("b.png")

    # New scheduler instance over the same files
    sched2 = ServeScheduler(mgr)
    stats_a = sched2.stats_for("a.png")
    assert stats_a is not None
    assert stats_a.total_show_count == 1
    last = sched2.last_served()
    assert last is not None and last[0] == "b.png"


def test_telemetry_record(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    sched.record_screen_call("frame-01", "192.168.1.5", 300, None, 3800, None)
    screens = sched.screens()
    assert "frame-01" in screens
    s = screens["frame-01"]
    assert s.ip == "192.168.1.5"
    assert s.request_count == 1
    assert s.battery_mv == 3800
    assert s.battery_percent is not None and 0 <= s.battery_percent <= 100


def test_orphan_dropped_on_pick(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.mark_served("a.png")
    # Remove a.png from disk + manager
    mgr.remove("a.png")
    sched.pick_next(Orientation.NEUTRAL)
    assert "a.png" not in sched.stats()


# ── Orientation filter tests ──────────────────────────────────────────────────


def test_pick_next_orientation_filter_landscape(app_config: AppConfig, make_test_image):
    # 40×30 = landscape, 30×40 = portrait
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    result = sched.pick_next(Orientation.LANDSCAPE)
    assert result == "land.png"


def test_pick_next_orientation_filter_portrait(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    result = sched.pick_next(Orientation.PORTRAIT)
    assert result == "port.png"


def test_pick_next_neutral_includes_all(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # NEUTRAL = no filter, so the first pick is whichever has lowest show_index (alphabetical tie-break)
    result = sched.pick_next(Orientation.NEUTRAL)
    assert result in ("land.png", "port.png")


def test_pick_next_neutral_image_eligible_under_any_filter(app_config: AppConfig, make_test_image):
    # 30×30 = square = NEUTRAL
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("square.png", (30, 30)),
        ],
    )
    assert sched.pick_next(Orientation.LANDSCAPE) == "square.png"
    assert sched.pick_next(Orientation.PORTRAIT) == "square.png"
    assert sched.pick_next(Orientation.NEUTRAL) == "square.png"


def test_pick_next_orientation_filter_no_match_returns_none(app_config: AppConfig, make_test_image):
    # Only portrait images; requesting landscape yields None
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("port.png", (30, 40)),
        ],
    )
    assert sched.pick_next(Orientation.LANDSCAPE) is None


def test_peek_next_no_side_effects(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    before = sched._next_for.copy()
    _ = sched.peek_next(Orientation.NEUTRAL)
    assert sched._next_for == before
    s = sched.stats_for("a.png")
    assert s is None or s.total_show_count == 0


def test_set_screen_config_preserves_all_fields(app_config: AppConfig):
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)

    cfg1 = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
    sched.set_screen_config("s1", cfg1)

    # Update only orientation — filter_by_orientation must survive
    current = sched.get_screen_config("s1")
    sched.set_screen_config("s1", replace(current, orientation=Orientation.PORTRAIT))
    updated = sched.get_screen_config("s1")
    assert updated.orientation == Orientation.PORTRAIT
    assert updated.filter_by_orientation is True


def test_unknown_screen_defaults_to_landscape(app_config: AppConfig):
    """A never-seen screen returns ScreenConfig() with the dataclass default."""
    mgr = SingleThreadedImageManager(app_config)
    sched = ServeScheduler(mgr)
    cfg = sched.get_screen_config("never-seen")
    assert cfg.orientation == Orientation.LANDSCAPE
    assert cfg.filter_by_orientation is False


def test_precompute_recomputes_all_orientations_on_mark_served(
    app_config: AppConfig, make_test_image
):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # After mark_served, _next_for should update for all orientations
    sched.mark_served("land.png")
    assert (
        sched._next_for[Orientation.LANDSCAPE] is not None
        or sched._next_for[Orientation.NEUTRAL] == "port.png"
    )
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"


def test_precompute_all_locked_landscape_only(app_config: AppConfig, make_test_image):
    # Only landscape images: NEUTRAL and LANDSCAPE point to same image, PORTRAIT is None
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
        ],
    )
    assert sched._next_for[Orientation.NEUTRAL] == "land.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] is None


def test_precompute_all_locked_mixed(app_config: AppConfig, make_test_image):
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("land.png", (40, 30)),
            ("port.png", (30, 40)),
        ],
    )
    # Both orientations have a candidate; NEUTRAL picks alphabetically first (both show_index=0)
    assert sched._next_for[Orientation.LANDSCAPE] == "land.png"
    assert sched._next_for[Orientation.PORTRAIT] == "port.png"
    assert sched._next_for[Orientation.NEUTRAL] in ("land.png", "port.png")


def test_precompute_all_locked_neutral_images_appear_in_all_slots(
    app_config: AppConfig, make_test_image
):
    # Square image (NEUTRAL) should be picked for all three orientation slots
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [
            ("square.png", (30, 30)),
        ],
    )
    assert sched._next_for[Orientation.NEUTRAL] == "square.png"
    assert sched._next_for[Orientation.LANDSCAPE] == "square.png"
    assert sched._next_for[Orientation.PORTRAIT] == "square.png"


# ── Per-screen forced next (show-next override) ───────────────────────────────


def test_set_next_for_screen_and_peek(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.set_next_for_screen("frame-1", "b.png")
    assert sched.peek_next_for_screen("frame-1") == "b.png"
    assert sched.peek_next_for_screen("frame-2") is None


def test_set_next_for_screen_not_ready_raises(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    with pytest.raises(ValueError):
        sched.set_next_for_screen("frame-1", "missing.png")


def test_mark_served_clears_screen_override(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.set_next_for_screen("frame-1", "b.png")
    # serving a DIFFERENT image to that screen keeps the override
    sched.mark_served("a.png", screen_name="frame-1")
    assert sched.peek_next_for_screen("frame-1") == "b.png"
    # serving the image to a different/unnamed screen keeps it too
    sched.mark_served("b.png")
    assert sched.peek_next_for_screen("frame-1") == "b.png"
    # serving the image to THAT screen consumes it
    sched.mark_served("b.png", screen_name="frame-1")
    assert sched.peek_next_for_screen("frame-1") is None


def test_screen_override_persisted_across_reload(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.set_next_for_screen("frame-1", "b.png")
    sched2 = ServeScheduler(mgr)
    assert sched2.peek_next_for_screen("frame-1") == "b.png"


def test_screen_override_dropped_when_image_deleted(app_config: AppConfig, make_test_image):
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    sched.set_next_for_screen("frame-1", "b.png")
    mgr.remove("b.png")
    sched.pick_next(Orientation.NEUTRAL)  # triggers _reconcile
    assert sched.peek_next_for_screen("frame-1") is None


def test_remove_screen_clears_override(app_config: AppConfig, make_test_image):
    _, sched = _setup(app_config, make_test_image, ["a.png"])
    sched.set_next_for_screen("frame-1", "a.png")
    sched.remove_screen("frame-1")
    assert sched.peek_next_for_screen("frame-1") is None


def test_screen_override_bypasses_orientation_filter(app_config: AppConfig, make_test_image):
    """A landscape image forced onto a portrait-filtered screen is still honoured."""
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [("land.png", (400, 300)), ("port.png", (300, 400))],
    )
    sched.set_screen_config(
        "frame-1", ScreenConfig(orientation=Orientation.PORTRAIT, filter_by_orientation=True)
    )
    sched.set_next_for_screen("frame-1", "land.png")
    assert sched.peek_next_for_screen("frame-1") == "land.png"


# ── cross-screen de-dup (two frames shouldn't show the same photo) ────────────
# De-dup now happens at COMMIT time, not serve time: each screen's committed next is
# resolved ahead so the frames-drawer ("up next") always equals the actual serve.


def _served(sched, screen, image):
    """Simulate a frame having been served `image` (sets its telemetry last_served)."""
    sched.mark_served(image, screen_name=screen)
    sched.record_screen_call(screen, "192.0.2.9", 300, image, None, None)


def _register(sched, screen):
    """Register a screen with telemetry so it participates in the per-screen commit."""
    sched.record_screen_call(screen, "192.0.2.9", 300, None, None, None)


def test_dedup_two_screens_get_different_images(app_config: AppConfig, make_test_image):
    """With >=2 ready images, a second frame's pick avoids the image the first frame
    is showing."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    first = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    assert first is not None
    _served(sched, "frame-1", first)
    # frame-2 must NOT be handed frame-1's image.
    second = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-2")
    assert second is not None
    assert second != first


def test_dedup_falls_back_to_repeat_when_one_image(app_config: AppConfig, make_test_image):
    """Only one ready image: both frames still get served it (a repeat beats serving
    nothing)."""
    _, sched = _setup(app_config, make_test_image, ["only.png"])
    first = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    assert first == "only.png"
    _served(sched, "frame-1", first)
    second = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-2")
    assert second == "only.png"  # graceful repeat, not None


def test_dedup_none_screen_keeps_shared_pick(app_config: AppConfig, make_test_image):
    """screen_name=None (UI preview / no frame context) keeps the original shared pick —
    no de-dup, deterministic least-shown choice."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    # Two calls with no screen context return the same pre-computed choice.
    assert sched.pick_next(Orientation.NEUTRAL) == sched.pick_next(Orientation.NEUTRAL)


def test_dedup_avoids_other_screens_pending_override(app_config: AppConfig, make_test_image):
    """An image queued (pending override) for another screen is also avoided."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    _register(sched, "frame-2")
    sched.set_next_for_screen("frame-1", "a.png")   # frame-1 has a.png queued
    picked = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-2")
    assert picked == "b.png"   # frame-2 avoids frame-1's queued image


# ── drawer == serve (the whole point) ─────────────────────────────────────────
# committed_next_for_screen(s) is what the drawer shows; pick_next(s) is what serves.
# They must be identical for every screen, in every state.


def test_committed_equals_serve_for_every_screen(app_config: AppConfig, make_test_image):
    """The committed value the drawer reads == what the next serve returns, per screen."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    _register(sched, "frame-2")
    # Drive a couple of serves so state advances and recommits happen.
    for _ in range(4):
        for scr in ("frame-1", "frame-2"):
            drawer = sched.committed_next_for_screen(scr)
            served = sched.pick_next(Orientation.NEUTRAL, screen_name=scr)
            assert drawer == served, f"{scr}: drawer {drawer} != serve {served}"
            _served(sched, scr, served)


def test_committed_two_landscape_frames_differ(app_config: AppConfig, make_test_image):
    """Two same-orientation frames commit to DIFFERENT images (no duplicate on the wall)."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    for scr in ("frame-1", "frame-2"):
        _register(sched, scr)   # default config: no orientation filter
    # Force a recommit and read both committed picks.
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    c1 = sched.committed_next_for_screen("frame-1")
    c2 = sched.committed_next_for_screen("frame-2")
    assert c1 is not None and c2 is not None
    assert c1 != c2


def test_committed_frame_does_not_repeat_or_starve(app_config: AppConfig, make_test_image):
    """A lone frame must ROTATE fairly through the whole library — never re-commit the image
    it just served while others are less-shown (regression: the commit path once let a frame
    re-pick its own just-served image, hogging one photo every wake)."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    counts = {"a.png": 0, "b.png": 0, "c.png": 0}
    last = None
    for _ in range(9):
        n = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
        assert n is not None
        assert n != last, f"frame repeated {n} back-to-back"
        _served(sched, "frame-1", n)
        counts[n] += 1
        last = n
    # 9 serves over 3 images with fair rotation ≈ 3 each; no image hogged (would be the bug).
    assert max(counts.values()) - min(counts.values()) <= 1, counts


def test_library_changed_repicks_only_the_screen_that_lost_its_image(app_config, make_test_image):
    """Deleting a committed 'up next' image + library_changed() immediately gives that screen a
    fresh valid pick, while a frame whose up-next wasn't deleted keeps its exact pick (passive,
    minimal-disturbance recommit)."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png", "d.png"])
    _register(sched, "frame-1")
    _register(sched, "frame-2")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")   # force a commit for the fleet
    c1 = sched.committed_next_for_screen("frame-1")
    c2 = sched.committed_next_for_screen("frame-2")
    assert c1 and c2 and c1 != c2

    mgr.remove(c1)                 # delete frame-1's committed up-next image
    sched.library_changed()

    new1 = sched.committed_next_for_screen("frame-1")
    assert new1 is not None and new1 != c1, "frame-1 must get a fresh valid pick"
    assert any(r.name == new1 for r in mgr.list()), "the new pick must be a real ready image"
    assert sched.committed_next_for_screen("frame-2") == c2, "frame-2 unaffected (minimal disturbance)"


def test_library_changed_drops_deleted_pin(app_config, make_test_image):
    """Deleting a PINNED image + library_changed() clears the dead pin."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    sched.set_next_for_screen("frame-1", "b.png")
    assert sched.committed_next_for_screen("frame-1") == "b.png"
    mgr.remove("b.png")
    sched.library_changed()
    assert sched.committed_next_for_screen("frame-1") in ("a.png", "c.png"), "dead pin must be gone"


def test_skip_next_is_per_frame_not_global(app_config, make_test_image):
    """Skipping frame-1's up-next rerolls frame-1 ONLY: frame-2 is untouched, and the skipped
    photo's GLOBAL show count is unchanged (per-frame penalty box, not a global show bump)."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png", "d.png"])
    _register(sched, "frame-1")
    _register(sched, "frame-2")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    c1 = sched.committed_next_for_screen("frame-1")
    c2 = sched.committed_next_for_screen("frame-2")
    assert c1 and c2 and c1 != c2
    idx_before = sched.stats_for(c1).show_index

    new1 = sched.skip_next("frame-1")
    assert new1 is not None and new1 != c1, "frame-1's up-next must change"
    assert sched.committed_next_for_screen("frame-2") == c2, "frame-2 must be untouched (per-frame)"
    assert sched.stats_for(c1).show_index == idx_before, "skip must NOT bump the global show count"


def test_skip_next_passes_this_round_then_returns_to_the_pool(app_config, make_test_image):
    """Skip means "not this one, now" — the photo sits out ONE pick, then it is back.

    It used to be boxed for a full cycle (eligible − 1), which scales with the library: at 184
    eligible photos and two refreshes a day, one tap removed a photo from that frame for about
    three months. Skipping keeps the photo's place in the rotation.
    """
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    skipped = sched.committed_next_for_screen("frame-1")
    replacement = sched.skip_next("frame-1")
    assert replacement != skipped, "the skip must actually change what's up next"

    # it sits out exactly this one pick...
    n = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    assert n != skipped
    sched.mark_served(n, screen_name="frame-1")

    # ...and is eligible again immediately after, with no cycle to wait out
    seen = set()
    for _ in range(6):
        n = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
        seen.add(n)
        sched.mark_served(n, screen_name="frame-1")
    assert skipped in seen, "a skipped photo must return to the pool, not be parked"


def test_skip_cooldown_does_not_scale_with_library_size(app_config, make_test_image):
    """The cost of a skip is fixed, so it can't grow into a months-long exile as photos are
    added — the bug that made one tap hide a photo for a quarter of a year."""
    _, sched = _setup(app_config, make_test_image, [f"p{i:02d}.png" for i in range(12)])
    _register(sched, "frame-1")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    skipped = sched.committed_next_for_screen("frame-1")
    sched.skip_next("frame-1")

    # one serve is all it takes to clear the box, whatever the pool size
    n = sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    sched.mark_served(n, screen_name="frame-1")
    assert not sched._active_skips_for("frame-1"), (
        "the box should be empty after a single serve regardless of library size"
    )
    assert skipped not in sched._active_skips_for("frame-1")


def test_skip_next_single_image_pool_is_noop(app_config, make_test_image):
    """Skipping with only one eligible photo just keeps it (nothing else to move to)."""
    _, sched = _setup(app_config, make_test_image, ["only.png"])
    _register(sched, "frame-1")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    assert sched.skip_next("frame-1") == "only.png"


def test_skip_survives_reload(app_config, make_test_image):
    """A per-frame skip persists across a restart (frames sleep for hours)."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png", "d.png"])
    _register(sched, "frame-1")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    skipped = sched.committed_next_for_screen("frame-1")
    sched.skip_next("frame-1")
    reloaded = ServeScheduler(mgr)
    assert skipped in reloaded._active_skips_for("frame-1"), "skip must survive reload"


def test_committed_no_dup_wins_over_no_repeat_when_scarce(app_config: AppConfig, make_test_image):
    """When the library is too small for every frame to have a distinct fresh pick, the
    NO-DUPLICATE rule (never the same photo on two frames at once) takes priority over the
    no-back-to-back-repeat preference. With 2 images and 2 frames, each frame keeps repeating
    its own image rather than ever colliding with the other frame."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png"])
    _register(sched, "frame-1")
    _register(sched, "frame-2")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    for _ in range(6):
        c1 = sched.committed_next_for_screen("frame-1")
        c2 = sched.committed_next_for_screen("frame-2")
        assert c1 != c2, "frames must never share an image, even when scarce"
        _served(sched, "frame-1", sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1"))
        _served(sched, "frame-2", sched.pick_next(Orientation.NEUTRAL, screen_name="frame-2"))


def test_committed_pin_wins_then_rotation_resumes(app_config: AppConfig, make_test_image):
    """A pin makes committed == pin; after it serves, committed resumes rotation."""
    _, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    sched.set_next_for_screen("frame-1", "b.png")
    assert sched.committed_next_for_screen("frame-1") == "b.png"
    assert sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1") == "b.png"
    _served(sched, "frame-1", "b.png")   # consumes the pin
    # Pin gone → committed is a normal rotation pick again (and still == serve).
    resumed = sched.committed_next_for_screen("frame-1")
    assert resumed is not None
    assert resumed == sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")


def test_committed_square_may_go_to_two_orientations(app_config: AppConfig, make_test_image):
    """A square/NEUTRAL image matches both filters, so it can legitimately be committed to a
    landscape AND a portrait frame at once — de-dup only blocks true cross-screen duplicates."""
    _, sched = _setup_with_sizes(
        app_config,
        make_test_image,
        [("sq.png", (500, 500))],   # single square image, matches every filter
    )
    sched.set_screen_config("land", ScreenConfig(
        orientation=Orientation.LANDSCAPE, filter_by_orientation=True))
    sched.set_screen_config("port", ScreenConfig(
        orientation=Orientation.PORTRAIT, filter_by_orientation=True))
    _register(sched, "land")
    _register(sched, "port")
    sched.pick_next(Orientation.LANDSCAPE, screen_name="land")
    assert sched.committed_next_for_screen("land") == "sq.png"
    assert sched.committed_next_for_screen("port") == "sq.png"


def test_committed_survives_reload(app_config: AppConfig, make_test_image):
    """Committed picks persist across a server restart (frames sleep for hours)."""
    mgr, sched = _setup(app_config, make_test_image, ["a.png", "b.png", "c.png"])
    _register(sched, "frame-1")
    sched.pick_next(Orientation.NEUTRAL, screen_name="frame-1")
    before = sched.committed_next_for_screen("frame-1")
    assert before is not None
    reloaded = ServeScheduler(mgr)   # re-reads the persisted DB
    assert reloaded.committed_next_for_screen("frame-1") == before


def test_committed_seeded_rng_is_deterministic(app_config: AppConfig, make_test_image):
    """Same seed → same per-screen commitment (sorted-screen iteration keeps it stable)."""
    names = [f"img_{i:02d}.png" for i in range(8)]

    def run(seed):
        upload = Path(app_config.upload_dir)
        for n in names:
            make_test_image(upload / n)
        mgr = SingleThreadedImageManager(app_config)
        mgr.sync()
        s = ServeScheduler(mgr, rng=random.Random(seed))
        for scr in ("frame-a", "frame-b", "frame-c"):
            _register(s, scr)
        s.pick_next(Orientation.NEUTRAL, screen_name="frame-a")
        return {scr: s.committed_next_for_screen(scr)
                for scr in ("frame-a", "frame-b", "frame-c")}

    assert run(7) == run(7)


# ── randomized least-shown tiebreak (spontaneity) ─────────────────────────────
# Rotation is still least-shown-first (fair), but ties are broken at RANDOM, not
# alphabetically. The RNG is injectable/seedable so these stay deterministic.

import random


def _setup_seeded(app_config, make_test_image, names, seed):
    upload = Path(app_config.upload_dir)
    for n in names:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    return mgr, ServeScheduler(mgr, rng=random.Random(seed))


def test_fairness_preserved_with_random_ties(app_config, make_test_image):
    """Every image shown exactly once per pass before any repeats — random ties don't
    break the least-shown guarantee."""
    names = [f"img_{i:02d}.png" for i in range(6)]
    _, sched = _setup_seeded(app_config, make_test_image, names, seed=1)
    seen = []
    for _ in range(6):
        n = sched.pick_next(Orientation.NEUTRAL)
        assert n is not None
        sched.mark_served(n)
        seen.append(n)
    # one full pass = every image exactly once (order randomized, coverage complete)
    assert sorted(seen) == sorted(names)
    # second pass also covers everything (fairness holds across cycles)
    seen2 = []
    for _ in range(6):
        n = sched.pick_next(Orientation.NEUTRAL)
        sched.mark_served(n)
        seen2.append(n)
    assert sorted(seen2) == sorted(names)


def test_ties_are_randomized_not_alphabetical(app_config, make_test_image):
    """With all images tied at show_index 0, the first pick is NOT forced to the
    alphabetically-first name for every seed — i.e. randomness is in effect."""
    names = [f"img_{i:02d}.png" for i in range(8)]
    first_picks = set()
    for seed in range(20):
        _, sched = _setup_seeded(app_config, make_test_image, names, seed=seed)
        first_picks.add(sched.pick_next(Orientation.NEUTRAL))
    # across 20 seeds we should see more than one distinct first pick (alphabetical
    # tie-break would give exactly one). Extremely unlikely to be 1 by chance.
    assert len(first_picks) > 1, f"expected varied first picks, got {first_picks}"


def test_seeded_rng_is_deterministic(app_config, make_test_image):
    """Same seed → same sequence (so tests and reproductions are stable)."""
    names = [f"img_{i:02d}.png" for i in range(8)]

    def run(seed):
        _, sched = _setup_seeded(app_config, make_test_image, names, seed=seed)
        out = []
        for _ in range(8):
            n = sched.pick_next(Orientation.NEUTRAL)
            sched.mark_served(n)
            out.append(n)
        return out

    assert run(42) == run(42)   # identical seed → identical order


# ── new-image handling: join at min, don't wipe history ───────────────────────
# Adding a photo must NOT reset every existing image's show_index (the old
# flatten-to-1 wiped rotation history on every upload). A new image starts at the
# current minimum show_index so it joins the least-shown tier fairly.


def test_new_image_does_not_wipe_existing_history(app_config, make_test_image):
    upload = Path(app_config.upload_dir)
    for n in ["a.png", "b.png", "c.png"]:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    sched = ServeScheduler(mgr, rng=random.Random(0))

    # Serve several rounds so images accumulate DIFFERENT show_index values.
    for _ in range(7):
        n = sched.pick_next(Orientation.NEUTRAL)
        sched.mark_served(n)
    before = {n: sched.stats_for(n).show_index for n in ["a.png", "b.png", "c.png"]}
    assert max(before.values()) >= 2, f"need varied history to test, got {before}"

    # Add a new image and reconcile.
    make_test_image(upload / "z_new.png")
    mgr.sync()
    sched.pick_next(Orientation.NEUTRAL)  # triggers _reconcile

    after = {n: sched.stats_for(n).show_index for n in ["a.png", "b.png", "c.png"]}
    # Existing images keep their history (NOT flattened to 1).
    assert after == before, f"existing history was altered: {before} -> {after}"
    # The new image joined at the minimum existing show_index.
    assert sched.stats_for("z_new.png").show_index == min(before.values())


def test_new_image_shows_within_next_cycle(app_config, make_test_image):
    """A newly added image (joined at min) appears within the next full pass, not starved."""
    upload = Path(app_config.upload_dir)
    for n in ["a.png", "b.png", "c.png"]:
        make_test_image(upload / n)
    mgr = SingleThreadedImageManager(app_config)
    mgr.sync()
    sched = ServeScheduler(mgr, rng=random.Random(3))
    for _ in range(6):  # two full passes
        sched.mark_served(sched.pick_next(Orientation.NEUTRAL))

    make_test_image(upload / "z_new.png")
    mgr.sync()
    seen = []
    for _ in range(4):  # next pass is 4 images now
        n = sched.pick_next(Orientation.NEUTRAL)
        sched.mark_served(n)
        seen.append(n)
    assert "z_new.png" in seen, f"new image should show within the next cycle, saw {seen}"


# ── multi-frame fleets (8-10 frames, mixed orientation filters) ───────────────
# The commit-time assignment is a bipartite MATCHING across the whole fleet. These guard the
# invariants that a naive greedy per-screen pick broke: fleet fairness (no starvation), the
# NO-DUPLICATE rule with squares (eligible to every filter), and drawer==serve at scale.


def _mixed_library(app_config, make_test_image, n_land, n_port, n_sq):
    upload = Path(app_config.upload_dir)
    names = []
    for i in range(n_land):
        make_test_image(upload / f"L{i}.png", size=(40, 30)); names.append(f"L{i}.png")
    for i in range(n_port):
        make_test_image(upload / f"P{i}.png", size=(30, 40)); names.append(f"P{i}.png")
    for i in range(n_sq):
        make_test_image(upload / f"S{i}.png", size=(40, 40)); names.append(f"S{i}.png")
    mgr = SingleThreadedImageManager(app_config); mgr.sync()
    return mgr, names


def _run_fleet(sched, frames, rounds, seed):
    """Simulate frames waking in random interleaved order. Returns (dup_events, global_shows).
    frames: list[(name, ScreenConfig)]."""
    for name, cfg in frames:
        sched.set_screen_config(name, cfg)
        sched.record_screen_call(name, "1.1.1.1", 300, None, None, None)
    display = {name: None for name, _ in frames}
    gshows: dict[str, int] = {}
    dup_events = 0
    rng = random.Random(seed)
    for _ in range(rounds):
        order = [name for name, _ in frames]
        rng.shuffle(order)
        for name in order:
            cfg = dict(frames)[name]
            orient = cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL
            drawer = sched.committed_next_for_screen(name)
            served = sched.pick_next(orient, screen_name=name)
            assert served == drawer, f"{name}: drawer {drawer} != serve {served}"
            display[name] = served
            gshows[served] = gshows.get(served, 0) + 1
            sched.mark_served(served, screen_name=name)
            sched.record_screen_call(name, "1.1.1.1", 300, served, None, None)
            # NO two frames DISPLAY the same image at once
            seen = {}
            for g, img in display.items():
                if img is None:
                    continue
                if img in seen:
                    dup_events += 1
                seen[img] = g
    return dup_events, gshows


def test_fleet_eight_unfiltered_no_dup_and_covers_library(app_config, make_test_image):
    """8 unfiltered frames over a TIGHT library (11 images): never the same photo on two frames
    at once, and every image still gets shown (no image permanently starved to zero).

    NOTE ON FAIRNESS: exact show-balance is NOT asserted here. By design (minimal-disturbance
    recommit) an idle frame keeps its committed pick until IT serves, so serving one frame never
    reshuffles another's "up next" — the drawer==serve guarantee. The cost is that in a cramped
    library (frames ≈ images) shows concentrate somewhat rather than perfectly balancing across
    the fleet; that is the accepted trade (drawer stability > tight-fleet balancing). Real fleets
    have far more images than frames, where both hold — see test_fleet_two_frames_plentiful_fair."""
    mgr, names = _mixed_library(app_config, make_test_image, 6, 4, 1)  # 11 images, 8 frames
    sched = ServeScheduler(mgr, rng=random.Random(7))
    frames = [(f"frame-{i}", ScreenConfig()) for i in range(8)]
    dup, gshows = _run_fleet(sched, frames, rounds=30, seed=13)
    assert dup == 0, f"{dup} same-image-on-two-frames events (hard rule)"
    assert len(gshows) == len(names), f"coverage {len(gshows)}/{len(names)} — an image was starved"
    assert min(gshows.values()) > 0, f"an image was never shown: {gshows}"


def test_fleet_two_frames_plentiful_fair_and_stable(app_config, make_test_image):
    """The REAL-WORLD shape: 2 frames, many images. Serving one frame must NEVER change the
    other's committed 'up next' (the reshuffle bug), drawer==serve holds, AND fairness is tight."""
    mgr, names = _mixed_library(app_config, make_test_image, 30, 30, 4)  # 64 images, 2 frames
    sched = ServeScheduler(mgr, rng=random.Random(4))
    L = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
    P = ScreenConfig(orientation=Orientation.PORTRAIT, filter_by_orientation=True)
    frames = [("E1003", L), ("Living Room", P)]
    for nm, cfg in frames:
        sched.set_screen_config(nm, cfg)
        sched.record_screen_call(nm, "1.1.1.1", 300, None, None, None)
    reshuffles = 0
    rng = random.Random(9)
    display = {nm: None for nm, _ in frames}
    gshows = {}
    for _ in range(40):
        order = [nm for nm, _ in frames]
        rng.shuffle(order)
        for nm in order:
            cfg = dict(frames)[nm]
            other = "Living Room" if nm == "E1003" else "E1003"
            other_before = sched.committed_next_for_screen(other)
            drawer = sched.committed_next_for_screen(nm)
            served = sched.pick_next(cfg.orientation, screen_name=nm)
            assert served == drawer, f"drawer!=serve {nm}: {drawer} vs {served}"
            sched.mark_served(served, screen_name=nm)
            sched.record_screen_call(nm, "1.1.1.1", 300, served, None, None)
            if sched.committed_next_for_screen(other) != other_before:
                reshuffles += 1
            display[nm] = served
            gshows[served] = gshows.get(served, 0) + 1
            assert display["E1003"] != display["Living Room"] or display["E1003"] is None, \
                "same image on both frames at once"
    assert reshuffles == 0, f"{reshuffles} cross-frame reshuffles — serving one frame changed the other's up-next"


def test_fleet_ten_mixed_filters_square_never_double_committed(app_config, make_test_image):
    """10 frames with mixed L/P/unfiltered filters: a square (eligible to every filter) must
    never be committed to two frames at once (regression: greedy assigned a square to two
    frames while other images sat unused)."""
    mgr, names = _mixed_library(app_config, make_test_image, 6, 4, 1)  # incl. one square
    sched = ServeScheduler(mgr, rng=random.Random(5))
    L = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
    P = ScreenConfig(orientation=Orientation.PORTRAIT, filter_by_orientation=True)
    U = ScreenConfig()  # unfiltered
    frames = [(f"frame-{i}", (L, P, U)[i % 3]) for i in range(10)]
    dup, _ = _run_fleet(sched, frames, rounds=30, seed=21)
    assert dup == 0, f"{dup} same-image-on-two-frames events across mixed filters"
    # committed values are distinct wherever distinct images exist (11 imgs >= 10 frames)
    committed = [sched.committed_next_for_screen(f) for f, _ in frames]
    committed = [c for c in committed if c is not None]
    assert len(committed) == len(set(committed)), f"duplicate committed values: {committed}"


def test_fleet_filtered_frames_only_serve_matching_orientation(app_config, make_test_image):
    """A landscape-filtered frame never serves a portrait image, and vice-versa (squares OK)."""
    mgr, names = _mixed_library(app_config, make_test_image, 4, 4, 2)
    sched = ServeScheduler(mgr, rng=random.Random(9))
    L = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
    P = ScreenConfig(orientation=Orientation.PORTRAIT, filter_by_orientation=True)
    frames = [("land-0", L), ("land-1", L), ("port-0", P), ("port-1", P)]
    for name, cfg in frames:
        sched.set_screen_config(name, cfg)
        sched.record_screen_call(name, "1.1.1.1", 300, None, None, None)
    rng = random.Random(2)
    for _ in range(20):
        order = [n for n, _ in frames]; rng.shuffle(order)
        for name in order:
            cfg = dict(frames)[name]
            served = sched.pick_next(cfg.orientation, screen_name=name)
            assert served is not None
            # landscape frames get L* or S*; portrait frames get P* or S*
            if cfg.orientation == Orientation.LANDSCAPE:
                assert served[0] in ("L", "S"), f"{name} served portrait {served}"
            else:
                assert served[0] in ("P", "S"), f"{name} served landscape {served}"
            sched.mark_served(served, screen_name=name)
            sched.record_screen_call(name, "1.1.1.1", 300, served, None, None)


def test_fleet_committed_survives_reload_stable(app_config, make_test_image):
    """After an 8-frame fleet warms up, a reload reproduces the SAME committed assignment
    (drawer doesn't thrash across a server restart)."""
    mgr, names = _mixed_library(app_config, make_test_image, 6, 4, 1)
    sched = ServeScheduler(mgr, rng=random.Random(4))
    frames = [(f"frame-{i}", ScreenConfig()) for i in range(8)]
    _run_fleet(sched, frames, rounds=10, seed=8)
    before = {f: sched.committed_next_for_screen(f) for f, _ in frames}
    reloaded = ServeScheduler(mgr, rng=random.Random(4))
    after = {f: reloaded.committed_next_for_screen(f) for f, _ in frames}
    assert after == before, f"reload changed committed assignment:\n before={before}\n after={after}"
