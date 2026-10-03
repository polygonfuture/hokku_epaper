# Port plan: this fork onto defl's 4.x

**Updated 2026-10-03.** Replaces the 2026-07-25 plan. Planning only; nothing has been ported yet.

**Where things stand:**
- **defl's `main`** is at `32e3640`: 4.0.0 beta 3, 376 commits past the shared base `27d4cbf` (3.1.0 alpha 1). There's no stable 4.0.0 yet.
- **This fork's `dev`** carries its own changes on the same base, listed in [FORK_CHANGELOG.md](FORK_CHANGELOG.md).

For what each side has, see [FORK_VS_UPSTREAM_MAIN.md](FORK_VS_UPSTREAM_MAIN.md); for dev's feature list, see [FORK_CHANGELOG.md](FORK_CHANGELOG.md).

## Approach

- **Re-port, don't merge.** 4.x moved the server to `python/hokku/webserver/`, replaced the panel constants with a `Display` class, keys renders by screen model, and split the firmware into `firmware/common/` plus board layers. A `git merge` would be conflict soup. Instead, build one branch per feature off defl's main and re-apply each feature, using `dev` as the reference.
- **Fixes first, as small PRs.** Each should be checked against his current main before opening.
- **Big features as proposals with a working reference.** Upstream has re-implemented large contributions in its own architecture before (the E1004 support, labels), so a working fork plus a clear write-up is the useful offer.

## What's no longer needed

defl's 4.x has its own version of these; take his and drop ours.

| Ours | His |
|---|---|
| Threaded Werkzeug (`71fb1b2`) | waitress |
| EXIF-rotated photo dimensions (`a46957a`) | Fixed, with a migration (issue #40) |
| Random tiebreak; new photos join the least-shown tier (`1a7f40b`, `66dc3b3`) | The same two fixes (#24) |
| Frame display name (`8e805a1`) | A real rename, with screens keyed by MAC |
| Face-aware cropping; manager `retire()` | Already his; ours were verbatim ports |
| Dither preset labels | His beta 3 named defaults |

## Phase 0: fixes to offer upstream

Each is still absent from his main as of `32e3640`, and each is small.

1. **Double refresh at scheduled times:** a grace window when picking the next slot (`4e40ee9`). It complements his drift calibration rather than replacing it.
2. **Fill limit:** a 150% ceiling and a 0.5% rounding snap (`d31f428`, `04f36dd`, `6192b5c`).
3. **Power-cut-safe state:** fsync, `.bak`, refusing to rebuild over an unreadable database, dated snapshots (`fae933e`).
4. **mDNS:** ignore VPN / virtual adapters, bind to the LAN IP, and a self-healing watchdog (`9b43cac`, `0a2ce7f`, `356a637`). `mdns.py` is otherwise untouched upstream, so it's a clean apply.
5. **Phantom "unnamed" frame** from health checks on the server host (`1322bd1`).
6. **Stuck upload overlay:** his page has the same `dragDepth` counter (`templates/index.html`, around line 4721) that `fd176ff` replaced.

Items 1–4 are written up in [UPSTREAM_BUGFIXES.md](UPSTREAM_BUGFIXES.md).

## Phase 1: the E1003 as a first-class screen

- **Server:** a `screens/seeedstudio_e1003/` entry with a mono `Display` (1872 x 1404, 16 greys, 4bpp wire packing), carrying dev's mono render and tone pipeline. The frame announces itself with `X-Screen-Model` instead of `X-Panel-Type`.
- **Firmware:** a board layer on `firmware/common/`, using his E1004 board (same baseboard family) as the template. Keep `it8951.c`, the charger driver and `pins.h`; OTA then comes for free. VCOM stays read-only.
- **Cutover:** the server and the E1003 firmware have to switch together.

## Phase 2: serving, on his scheduler

- **Committed up next per frame,** with fleet matching, so the drawer's up next is exactly what is served (`1340119`). It's compatible with his random tiebreak.
- **Skip up next** (`b15abc2`) and **send to a specific frame** (`cb58c28`). These sit next to his global Show next and per-frame labels.
- **Interval refresh** (`ced38d3`): upstream has only specific times. `time_utils.py` is otherwise unchanged upstream, so it's a clean add.
- **Smaller items:** debug fast-refresh that doesn't spend turns (`95e6325`), up-next refresh after delete and conversion (`0fcfd73`, `b140d20`), and the serve-decision log (`42a0f19`).

## Phase 3: editor and web app

- **Per-picture settings:** build on his override model (dither and fill limit per picture). Add crop and rotation, the mono version, and the editor's preview endpoints. Weave the `edit_*` fields into his record loader before the first run, or stored edits are lost.
- **The new web app** (`/hokku/app`): no upstream counterpart. Port the static bundle and its endpoints.
- **Ask first:** these are opinionated UI changes, so ask defl about appetite before shaping them as PRs; they may stay fork-only.

## Phase 4: long tail

- **Windows installer:** `feat/local-pc-server-install` (`5223f68`) is an old branch. It isn't in `dev` and won't be merged; it may be worth a look later. Its refresh-schedule changes (`2d688fe`) are superseded and won't be used.
- **Docs:** move the manual and the E1003 docs into his per-screen docs layout.

## Open items

- **Timing:** wait for a stable 4.0.0 tag, or start an integration branch now on his latest main.
- **Contact:** whether to raise the plan with defl before the first PR.
