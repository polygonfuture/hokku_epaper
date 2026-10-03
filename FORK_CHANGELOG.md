# Fork changes: `dev` vs upstream 3.1.0 alpha 1

**Fork:** polygonfuture/hokku_epaper, branch `dev` · **Base:** defl/hokku_epaper `main` at `27d4cbf` (3.1.0 alpha 1, 2026-05-30) · **86 commits**, 2026-07-16 to 2026-10-03

The fork's own `main` is an untouched mirror of that base. Nothing here has been merged with 4.x; the plan is to re-port features one at a time onto a stable 4.x release rather than merge. Two pieces were ported *from* 4.0.x verbatim (face-aware cropping, the manager's `retire()`), so a later port sees them as no-ops.

Untouched: the classic web page (`/hokku/ui`), the Spectra 6 firmware, and every existing config key and preset name. New config keys are additive.

## Bugs that also affect upstream

Each one is written up in [UPSTREAM_BUGFIXES.md](UPSTREAM_BUGFIXES.md) as a description to re-implement in 4.x, not a patch.

1. **Double refresh at scheduled times.** RTC drift wakes a frame just before a slot, so it naps and serves the same slot twice. Fixed with a 5-minute grace window. (`4e40ee9`)
2. **Zoom to fill can't fill real 3:2 photos.** The slider stops at 100%, but real sensors are 1.500–1.512, and whole-pixel rounding tips exact cases into letterbox. Fixed with a 150% ceiling and a 0.5% snap. (`d31f428`, `04f36dd`, `6192b5c`)
3. **A power cut can erase the image database.** An unflushed rename read back empty and was then saved over. Fixed with fsync and a `.bak`, guarded loads, refusing to rebuild over an unreadable file, and dated snapshots. (`fae933e`)
4. **`hokku.local` stops resolving after a Wi-Fi or VPN reconnect.** Windows drops the multicast membership. Fixed with a watchdog that probes and re-registers. (`356a637`)

## New web app (`/hokku/app`)

Mobile-first, alongside the classic page; `"default_ui"` picks which one `/` opens. (`d3bf759`, `a5e5613`)

- **Gallery:** six-colour thumbnails, size slider, Mixed / Landscape / Portrait. From 60 photos, Sort (including longest unseen, grouped by age) and Show (B&W / People / Non-People). (`723b704`, `87e9249`, `1d903ef`)
- **Photo detail:** Spectra 6 / Mono 16 / Original views, ← / → between photos, Download original. (`04a3e82`, `f85bc61`)
- **Send to a frame:** a chosen photo goes to one specific frame at its next refresh. (`cb58c28`)
- **Uploads:** progress and conversion status in the header; a single photo opens in the editor as a draft. (`8367f95`, `fd176ff`)
- **Settings:** four pages. Image Rendering uses 4.x's section and label names. (`7fa1855`)
- **Refresh schedule:** besides specific times, an Interval mode (every 1 to 24 hours, optionally only during active hours). (`ced38d3`)
- **Originals in any format:** a new `/display` serves a browser-safe render of TIFF / HEIC / JXL; `/original` stays raw. (`96fb2bb`)

## Per-image photo editor

- Crop to the frame's 4:3 / 3:4, rotate, and per-photo dither settings. The live preview is the real render: a fast draft, then full resolution. Fit / 100% zoom, and press and hold for the original. (`69f1e68`, `97c0ed3`, `4847582`, `2cfe4df`, `42a51b4`)
- Non-destructive and re-editable. A crop applies only to frames of the shape it was made for; other frames render the original. (`502cb87`)

## Frames

- **Drawer cards:** what's showing, battery, next and last check-in, Overdue. An equal-width grid on any screen, scaled up on QHD / 4K. (`327a42a`, `edc26e4`)
- **Frame preview:** the photo exactly as that frame renders it, on Now or Up next, with a fit line (Exact fit / Zoomed n% / Needs n%). (`67460b8`, `825eca6`, `6c6a32f`, `75c9c8e`)
- **Skip up next, per frame:** deals from a shuffled deck, and never touches show counts or other frames. (`b5109c1`, `9d1a3e9`, `b15abc2`)
- **Rename and panel type:** a display name (the provisioned name stays the key) and a per-screen panel type. Frame colours are colour-blind safe. (`8e805a1`, `f28da0b`)

## reTerminal E1003 (10.3" mono, 16 greys)

- **Firmware:** a separate ESP-IDF project in `firmware-e1003/`, covering the IT8951 driver, deep sleep, battery and the wake button. Validated on hardware. It keeps the last photo when a fetch fails. (`dec1b9a`, `b4bcb2c`, `16627be`)
- **Server:** a mono render path that runs only for `X-Panel-Type: mono16_e1003`, so colour output and caches are unchanged. (`0615ce3`, `8c62c48`)
- **Tone:** profiles (Faithful, B&W Contrast, Custom) with a live tone editor. The photo editor can tune a photo's mono version, and its crop, separately. (`7c66548`, `a13e34a`, `7be2d7e`, `b8c74d8`)

## Framing

- **One rule for colour and mono,** `frame_decision()`: content upright for the frame, and Zoom to fill alone decides fill or letterbox. (`a13e34a`, `ceac12d`)
- **Face-aware cropping,** ported from 4.0.x. It's on by default here, also drives the mono path, and holds through manual crops. (`ceac12d`)
- **EXIF-rotated phone portraits** were recorded as landscape, which broke orientation filtering and the send warning. (`a46957a`)
- **The orientation filter** uses a crop's effective orientation. (`d4cdfa0`)

## Serving and rotation

- **Up next is committed ahead and is exactly what the frame serves.** Frames are assigned by a matching, so two frames never show the same photo unless the library is too small, and serving one frame never reshuffles another's up next. (`1340119`)
- **Fairer rotation:** ties between least-shown photos break at random, not alphabetically. A new photo joins the least-shown tier instead of resetting everyone's history. (`1a7f40b`, `66dc3b3`)
- **Up next stays valid** after a delete and after conversions finish. (`0fcfd73`, `b140d20`)
- **Debug fast-refresh** no longer spends rotation turns. (`95e6325`)
- **Serve-decision log:** optional, for diagnosing drawer-vs-serve; off by default. (`42a0f19`)

## Server reliability

- **Threaded request handling,** so a slow render can't block other requests. (`71fb1b2`)
- **Config reload** retires the old image manager instead of letting its final flush overwrite the new one. Ported from 4.0.x. (`2cfa01b`)
- **mDNS** skips VPN and virtual adapters, and binds to the LAN IP only. (`9b43cac`, `0a2ce7f`)
- **Phantom frames:** health checks from the server host no longer register an "unnamed" frame. (`1322bd1`)

## Docs and tests

- **User manual:** a new web-app chapter covering all of the above, with screenshots from a demo library. (`34f6c0e`, `8ef3fac`, `a8b9b13`, `d9a7537`)
- **Tests:** 866 pass, 5 skipped. New suites cover the framing truth table, mono render agreement, crash-safe state, scheduler fairness and Skip. `tools/stress_multiframe.py` checks 120 multi-frame configurations.

## Left for the 4.x port

- **Mono rendering** still runs in the request handler with no disk cache. 4.x already generalises displays, so this waits for the port.
