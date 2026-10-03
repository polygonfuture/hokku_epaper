# Fork `dev` vs defl's current `main`

**Compared:** polygonfuture/hokku_epaper `dev` and defl/hokku_epaper `main` at `32e3640` (4.0.0 beta 3, 2026-10-02), on 2026-10-03.

Both grew from the same 3.1.0 alpha 1 (`27d4cbf`, 2026-05-30) and have moved apart since: **86 commits on dev, 376 on defl's main**. Neither contains the other, apart from two pieces dev ported from 4.0.x and a handful of fixes both sides made on their own. defl's 4.x also moved the server from `webserver/hokku_server/` to `python/hokku/webserver/`, so dev can't simply be merged; features have to be re-ported one at a time.

For dev's full feature list see [FORK_CHANGELOG.md](FORK_CHANGELOG.md).

## At a glance

| Area | dev | defl's main |
|---|---|---|
| Web interface | New mobile-first app at `/hokku/app`; classic page unchanged from 3.1 | Classic page, much extended: per-frame Config, Admin, Flash a screen, Firmware library |
| Screens | Hokku 13.3" Spectra 6; **reTerminal E1003** (10.3" mono, 16 greys) | Hokku / Huessen 13.3"; **Bigme F7** 7.3"; **reTerminal E1004** (13.3" Spectra 6, experimental) |
| Server install | As in 3.1 | **Raspberry Pi appliance image** with a setup wizard; pip wheel |
| Firmware updates | USB only, as in 3.1 | **Over the air**, per screen; flash from the browser; download from GitHub |
| Tuning one photo | Editor: crop, rotate, dither, E1003 tone | Per-picture dither and fill limit |
| Picking photos per frame | Orientation filter; send to a frame; skip up next | Orientation filter; **labels** (tags on photos, filter per frame); show next |
| Colour rendering | As in 3.1 | **Measured panel model**, S-curve tone, calmer colour, new defaults |

## Built on both sides

Where the two overlap. For a port, the last column says which version to keep.

| Feature | dev | defl's main | For a port |
|---|---|---|---|
| Per-picture settings | Crop rectangle, rotation, dither, and an E1003 version, in an editor with a live preview. `POST /image/<name>/edit` | Dither and fill limit per picture. `/image/<name>/config` | His model as the base, plus our crop, rotation and editor |
| Frame rename | A display name; the frame keeps its provisioned name | A real rename: the frame learns its new name, and screens are keyed by MAC | His |
| Phone portraits (EXIF rotation) | Fixed (`a46957a`) | Fixed independently, with a migration (issue #40) | His |
| Rotation fairness | Random tiebreak; new photos join the least-shown tier (`1a7f40b`, `66dc3b3`) | The same two fixes (#24) | His |
| One slow request blocking others | Threaded Werkzeug (`71fb1b2`) | waitress | His |
| Face-aware cropping; manager `retire()` | Ported from his 4.0.x, verbatim | Original | Identical |
| Frames refreshing twice | Server grace window around each scheduled slot (`4e40ee9`) | Frames learn their own sleep-timer drift | Both: his makes wakes accurate, ours makes a slightly early wake harmless |
| Zoom to fill / Letterbox fill limit | Same key and rule; up to 150%, with a rounding snap; global only | Up to 100%; per frame and per picture too | His per-frame and per-picture values, plus our ceiling and snap |
| reTerminal panels | E1003 (mono): a separate firmware project, plus a server hook on `X-Panel-Type` | E1004 (colour): a board layer on `firmware/common/`, plus a `Display` registry entry on `X-Screen-Model` | E1003 becomes a screen entry and a board layer in his structure |
| Dither presets | The three from 3.1, unchanged | The same three, with "General / Black & white / Faces (default)" listed first (beta 3) | His |
| HEIC / TIFF / JXL | Rendered, and viewable in the browser via `/display` | Rendered; the original is served raw | Ours for viewing |

## Only in dev

- **New web app:** gallery Sort / Show, a detail view with a Mono 16 tab, ← / →, Download original, and Upload & edit. (Its own manual chapter.)
- **Photo editor:** crop and rotate, live preview from a fast draft to full resolution, Fit / 100% zoom, and press and hold for the original.
- **Frames:** drawer cards; a frame preview showing exactly what a frame renders, with a fit line; Skip up next per frame; send a photo to one specific frame.
- **E1003 support:** firmware, the mono render path, tone profiles and a live tone editor.
- **Refresh schedule:** an Interval mode (every 1 to 24 hours, with optional active hours) alongside specific times.
- **Serving:** each frame's up next is committed ahead and is exactly what it serves, with fleet matching so frames don't repeat each other. Up next refreshes after deletes and conversions. Debug fast-refresh doesn't spend turns. An optional serve-decision log.
- **Reliability:** power-cut-safe state (fsync, `.bak`, snapshots); mDNS ignores VPN adapters and has a self-healing watchdog; no phantom "unnamed" frame from health checks.

## Only in defl's main

- **Raspberry Pi appliance image** with a captive-portal setup wizard and recovery watchdogs.
- **Multi-screen platform:** Bigme F7 (reverse-engineered XR872), E1004, and per-model rendering and caching via a `Display` registry.
- **Firmware:** over-the-air updates with rollback, flashing a screen from the browser, a GitHub firmware library with per-model pinning, and USB whole-image upload.
- **Frames:** sleep-drift calibration, MAC-keyed identity, and a per-frame Config (orientation, fill limit, OTA, server URL).
- **Labels:** free-form tags on photos, with a per-frame label filter.
- **Colour:** a measured panel model (1,733 readings plus 443 blind ratings), a bounded S-curve, per-channel autocontrast off by default, and new Atkinson + OKLAB defaults.
- **Fixes:** the config loader collapsing all three pipelines into one, firmware version ordering, appliance reboot freezes, and packaging.

## Still open upstream

dev's four write-ups in [UPSTREAM_BUGFIXES.md](UPSTREAM_BUGFIXES.md) are all still absent from his `main` as of `32e3640`:

1. No grace window when picking the next scheduled slot.
2. The fill limit is capped at 100%, with no rounding snap.
3. No fsync or backup when saving state.
4. No mDNS watchdog.

Drift calibration (#1) and the config-reload fix are related but separate.
