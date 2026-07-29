"""Multi-frame stress test for the commit-time serve architecture (SERVE_COMMIT_ARCHITECTURE_PLAN.md).

Drives the REAL ServeScheduler (fake in-memory manager) through 120 combinations of:
  - frame count (2, 3, 5, 8, 10)
  - orientation-filter mixes (all-unfiltered, all-landscape, half-L/half-P, mixed L/P/unfiltered)
  - library sizes vs frame count (scarce, comfortable, plentiful, square-heavy, all-square, no-portrait)

Frames wake in random/interleaved order for 40 rounds each. HARD invariants (a FAIL if breached):
  - drawer == serve: committed_next_for_screen(s) == what pick_next(s) returns, per screen.
  - no AVOIDABLE dup-on-wall: two frames never DISPLAY the same image when distinct eligible
    images >= contending frames.
  - orientation filter respected: a filtered frame never serves an ineligible image.
  - no AVOIDABLE back-to-back repeat: a frame's own eligible pool >= 2x its contention.
Fairness (global show-spread, repeat rate) is reported as a HEALTH metric, not a hard fail —
in a degenerate library (images ~ frames) some repeats/imbalance are structurally forced.

Run from the repo root (uses its own location to find webserver/):
  .venv/Scripts/python.exe tools/stress_multiframe.py
Exit code 0 = PASS.
"""
from __future__ import annotations
import itertools, random, sys, tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Repo root = this file's parent's parent (tools/ -> repo). Portable, no hardcoded path.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "webserver"))

from hokku_server.image_record import ConvertStatus, ImageRecord
from hokku_server.orientation import Orientation
from hokku_server.screen_config import ScreenConfig
from hokku_server.serve_scheduler import ServeScheduler


# ── fake manager ────────────────────────────────────────────────────────────
@dataclass
class FakeConfig:
    cache_dir: str


class FakeManager:
    def __init__(self, records, cache_dir):
        self._records = {r.name: r for r in records}
        self.config = FakeConfig(cache_dir=cache_dir)

    def list(self):
        return list(self._records.values())

    def status(self, name):
        return self._records.get(name)


def _rec(name, w, h):
    return ImageRecord(
        name=name, name_hash=name, original_sha1=name, original_size_bytes=1,
        original_mtime=0.0, added_at=0.0, convert_status=ConvertStatus.OK,
        convert_error=None, image_width=w, image_height=h,
    )


LAND = (1200, 800)
PORT = (800, 1200)
SQ = (1000, 1000)


def make_library(n_land, n_port, n_sq):
    recs = []
    for i in range(n_land):
        recs.append(_rec(f"L{i}.png", *LAND))
    for i in range(n_port):
        recs.append(_rec(f"P{i}.png", *PORT))
    for i in range(n_sq):
        recs.append(_rec(f"S{i}.png", *SQ))
    return recs


def eligible_names(records, cfg: ScreenConfig):
    if not cfg.filter_by_orientation:
        return {r.name for r in records}
    return {r.name for r in records if r.matches_orientation_filter(cfg.orientation)}


# ── simulation ──────────────────────────────────────────────────────────────
@dataclass
class Frame:
    name: str
    cfg: ScreenConfig
    displaying: str | None = None
    last_served: str | None = None
    shows: dict = field(default_factory=dict)


def peek_orientation(cfg):
    return cfg.orientation if cfg.filter_by_orientation else Orientation.NEUTRAL


def run_config(label, frames_cfg, records, rounds, seed):
    """frames_cfg: list[(name, ScreenConfig)]. Returns (hard_violations, health)."""
    rng = random.Random(seed)
    # Max frames that ever contend for the SAME image = frames whose eligible pool overlaps.
    # A dup / repeat is only avoidable when a frame's pool has more images than that contention.
    def contention(name):
        fe = _elig_cache[name]
        return sum(1 for g, _ in frames_cfg if _elig_cache[g] & fe)

    with tempfile.TemporaryDirectory() as tmp:
        mgr = FakeManager(records, tmp)
        sched = ServeScheduler(mgr, rng=random.Random(seed))
        for name, cfg in frames_cfg:
            sched.set_screen_config(name, cfg)
            sched.record_screen_call(name, "1.2.3.4", 300, None, None, None)

        frames = {name: Frame(name, cfg) for name, cfg in frames_cfg}
        elig = {name: eligible_names(records, cfg) for name, cfg in frames_cfg}
        violations = []          # HARD invariant breaches only (drawer==serve, avoidable dup)
        repeats = 0              # back-to-back repeats (fairness health, not a hard fail)
        serves = 0
        # A repeat is AVOIDABLE only where a frame's own eligible pool is comfortably bigger
        # than the frames contending for it (pool >= 2*contention). Track worst-case per frame.
        avoidable_repeats = 0
        global_shows = {}        # image -> total shows across all frames (fairness signal)

        for rnd in range(rounds):
            order = list(frames.values())
            rng.shuffle(order)
            for f in order:
                pool = len(elig[f.name])
                cont = contention(f.name)
                drawer = sched.committed_next_for_screen(f.name)
                orient = peek_orientation(f.cfg)
                forced = sched.peek_next_for_screen(f.name)
                served = forced if forced is not None else sched.pick_next(orient, screen_name=f.name)

                # I1 drawer == serve — ALWAYS a hard invariant.
                if served != drawer:
                    violations.append(f"[{label}] I1 drawer!=serve {f.name}: drawer={drawer} served={served}")
                if served is None:
                    if pool > 0:
                        violations.append(f"[{label}] served None despite {pool} eligible for {f.name}")
                    continue
                # I3 filter respected — ALWAYS hard.
                if served not in elig[f.name]:
                    violations.append(f"[{label}] I3 {f.name} served ineligible {served}")
                # Back-to-back repeat: a fairness-health signal. It is AVOIDABLE (a real bug)
                # only when THIS frame's eligible pool is comfortably bigger than its contention
                # (pool >= 2*cont); otherwise it is structurally forced by scarcity.
                serves += 1
                if served == f.last_served:
                    repeats += 1
                    if pool >= 2 * cont:
                        avoidable_repeats += 1

                f.last_served = served
                f.displaying = served
                f.shows[served] = f.shows.get(served, 0) + 1
                global_shows[served] = global_shows.get(served, 0) + 1
                sched.mark_served(served, screen_name=f.name)
                sched.record_screen_call(f.name, "1.2.3.4", 300, served, None, None)

                # I2 dup-on-wall — hard ONLY when avoidable (enough distinct images to go around).
                shown = {}
                for g in frames.values():
                    if g.displaying is None:
                        continue
                    if g.displaying in shown:
                        gc = max(contention(g.name), contention(shown[g.displaying]))
                        gp = max(len(elig[g.name]), len(elig[shown[g.displaying]]))
                        # avoidable if the shared pool has room for every contending frame
                        distinct_shared = len(elig[g.name] | elig[shown[g.displaying]])
                        if distinct_shared >= gc:
                            violations.append(
                                f"[{label}] I2 AVOIDABLE dup-on-wall '{g.displaying}' "
                                f"{shown[g.displaying]}&{g.name} (distinct={distinct_shared} cont={gc})")
                    shown[g.displaying] = g.name

        # Fairness HEALTH (reported, not a hard failure): global spread across all shows, and
        # whether any image was starved (0 shows) while the library was small enough that it
        # should have appeared.
        gvals = list(global_shows.values())
        n_imgs = len(records)
        shown_imgs = len(global_shows)
        health = {
            "global_spread": (max(gvals) - min(gvals)) if gvals else 0,
            "coverage": f"{shown_imgs}/{n_imgs}",
            "avg": round(sum(gvals) / len(gvals), 1) if gvals else 0,
            "repeat_rate": round(repeats / serves, 3) if serves else 0.0,
            "avoidable_repeats": avoidable_repeats,
            "n_imgs": n_imgs, "n_frames": len(frames),
        }
        return violations, health


def _frames_sharing(frame, frames):
    """How many frames share (overlapping eligibility with) this frame — a lower bound on
    how many distinct images are 'reserved' away from it at any moment."""
    fe = _elig_cache[frame.name]
    n = 0
    for g in frames.values():
        if _elig_cache[g.name] & fe:
            n += 1
    return n


_elig_cache = {}


def main():
    random.seed(0)
    configs = []

    # library variants: (n_land, n_port, n_sq)
    libs = {
        "scarce":      (2, 1, 0),      # very few images
        "comfortable": (6, 4, 1),
        "plentiful":   (20, 15, 3),
        "square-heavy":(1, 1, 6),      # lots of squares (eligible everywhere)
        "all-square":  (0, 0, 8),      # only squares
        "no-portrait": (10, 0, 0),     # portrait-filtered frames will find only... nothing/squares
    }
    frame_counts = [2, 3, 5, 8, 10]

    def orient_mixes(n):
        # produce a few representative config lists for n frames
        L = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=True)
        P = ScreenConfig(orientation=Orientation.PORTRAIT, filter_by_orientation=True)
        U = ScreenConfig(orientation=Orientation.LANDSCAPE, filter_by_orientation=False)
        yield "all-unfiltered", [U] * n
        yield "all-landscape",  [L] * n
        yield "half-L-half-P",  [L if i % 2 == 0 else P for i in range(n)]
        yield "mixed-LPU",      [(L, P, U)[i % 3] for i in range(n)]

    all_viol = []
    summary = []
    for (libname, (nl, np_, ns)), fcount in itertools.product(libs.items(), frame_counts):
        records = make_library(nl, np_, ns)
        for mixname, cfgs in orient_mixes(fcount):
            frames_cfg = [(f"frame-{i}", cfgs[i]) for i in range(fcount)]
            global _elig_cache
            _elig_cache = {name: eligible_names(records, cfg) for name, cfg in frames_cfg}
            label = f"{libname}/{fcount}f/{mixname}"
            v, health = run_config(label, frames_cfg, records, rounds=40, seed=1234)
            all_viol.extend(v)
            summary.append((label, len(v), health))

    print("=" * 78)
    print(f"Ran {len(summary)} configurations x 40 rounds each.")
    print("=" * 78)
    bad = [s for s in summary if s[1] > 0]
    if bad:
        print(f"\n{len(bad)} configs with HARD (avoidable) VIOLATIONS:")
        for label, nv, h in bad:
            print(f"  {label}: {nv}")
        print("\nSample violations:")
        for msg in all_viol[:30]:
            print("  " + msg)
    else:
        print("\nALL HARD INVARIANTS HELD across every config:")
        print("  drawer==serve; no AVOIDABLE dup-on-wall; filter respected; no AVOIDABLE repeat.")

    # REAL repeat bug = an AVOIDABLE repeat (a frame's own eligible pool >= 2*its contention,
    # so a non-repeat pick was always achievable). Filter-aware and per-frame.
    repeat_bugs = [(l, h) for (l, nv, h) in summary if h["avoidable_repeats"] > 0]
    print("\nAVOIDABLE back-to-back repeats (frame's eligible pool >= 2*contention — should be none):")
    if repeat_bugs:
        for label, h in repeat_bugs:
            print(f"  {h['avoidable_repeats']} avoidable repeats  {label}")
    else:
        print("  NONE — every frame rotates without repeating whenever its own pool allows it.")

    print("\nFairness HEALTH — worst global show-spread (comfortable+ libs):")
    health_rows = [(l, h) for (l, nv, h) in summary
                   if l.split("/")[0] in ("comfortable", "plentiful", "square-heavy")]
    for label, h in sorted(health_rows, key=lambda x: -x[1]["global_spread"])[:12]:
        print(f"  spread={h['global_spread']:3d} cov={h['coverage']:>7} rep={h['repeat_rate']:<5}  {label}")

    hard_fail = bool(all_viol) or bool(repeat_bugs)
    print("\nRESULT:", "PASS" if not hard_fail else
          f"FAIL ({len(all_viol)} hard viol, {len(repeat_bugs)} repeat bugs)")
    return 0 if not hard_fail else 1


if __name__ == "__main__":
    sys.exit(main())
