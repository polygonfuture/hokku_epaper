# Multiple Screens

One server can drive as many frames as you like. This doc explains how the server sees multiple frames, what each frame does and doesn't have its own of, and how to steer which photos land where. For the general web app tour see [manual.md](manual.md); for setup see [install.md](install.md).

## Contents

1. [The mental model](#1-the-mental-model)
2. [How frames are identified](#2-how-frames-are-identified)
3. [The Screens dashboard](#3-the-screens-dashboard)
4. [Per-screen orientation](#4-per-screen-orientation)
5. [Steering which photos go where](#5-steering-which-photos-go-where)
6. [Removing a frame](#6-removing-a-frame)

---

## 1. The mental model

The single most important thing to understand: **every frame draws from one shared photo library.** There are no per-frame albums, playlists, folders, tags, or groups. When any frame wakes and asks the server for an image, the server hands it the next one from the same global pool, chosen by a fair-rotation scheduler that gives every photo equal screen time over the long run.

The design intent is that frames are interchangeable windows onto your whole collection. Hang three frames around the house and each one cycles through the same library independently, so you see different photos on each at any given moment simply because they check in at different times and are at different points in the rotation. You don't curate per-frame; you curate one library and let all the frames share it.

The **only** thing a frame carries that is genuinely its own is its **orientation** (and a related filter). Everything else — the refresh schedule, the dither settings, the image pool — is global and applies to every frame equally.

If you were expecting "assign these ten photos to the kitchen frame and those to the hallway frame," that concept does not exist in the current software. [Section 5](#5-steering-which-photos-go-where) covers what you *can* do to influence placement.

## 2. How frames are identified

A frame identifies itself to the server by the **screen name** you gave it during setup. That name travels in a header on every request the frame makes; the server has no other notion of device identity — no MAC address, no serial number, no pairing step, no token. The name is the whole identity.

Two consequences follow from this:

- **Give each frame a distinct name.** If you accidentally configure two frames with the same name, the server treats them as one frame — their telemetry overwrites each other and they share a single row in the dashboard. Names are set with `python tools/hokku_setup.py` when you configure the frame.
- **Frames appear automatically.** There's no "add a screen" button. A frame shows up in the Screens dashboard the first time it connects and asks for an image, and not before. A frame you've flashed but that hasn't yet completed a refresh won't be listed yet.

## 3. The Screens dashboard

The Screens tab lists every frame that has ever connected, one row per name, sorted alphabetically. Each row shows that frame's own telemetry, reported fresh on its last check-in:

- **Name** — the screen name from setup; how you tell frames apart.
- **IP** — the frame's address on your network at last check-in.
- **Battery** — percentage with a colour indicator; turns red below 20%.
- **Requests** — how many times this frame has fetched an image.
- **Last Seen** — when it last checked in. A frame on a 12-hour sleep will read "12 hours ago," which is normal.
- **Next Update** — when the server expects it back, from the refresh schedule and the sleep it was told to use. A frame that doesn't reappear by then is flagged overdue, and a warning banner summarises all overdue frames at the top of the tab.
- **Last Image** — a thumbnail of the photo this frame is currently showing.
- **Orientation** / **Match orient.** — the two per-screen settings, covered next.
- **Details** — a modal with the frame's self-reported diagnostics (firmware version, boot count, wake cause, free memory) and its recent firmware log.

Because frames don't hold a persistent connection — they wake, fetch, and go straight back to deep sleep — every value in a row is only as current as that frame's last check-in. The dashboard reflects the last thing each frame told the server, not live state.

## 4. Per-screen orientation

Orientation is the one setting each frame owns independently, and it's set right in the dashboard row.

**Orientation** (Landscape / Portrait) tells the server how this particular frame is mounted, so it can render images the right way up for that frame. A frame hung in portrait can show portrait-rendered images while another in landscape shows the same library rendered for landscape, all from the same pool. A brand-new frame defaults to **Landscape** until you change it.

**Match orient.** is a checkbox that turns orientation into a *filter*. With it off (the default), the frame is eligible to receive any image in the library, rendered to fit. With it on, the frame only receives images whose native orientation matches its own — a portrait frame with the box ticked will only be served photos that were themselves shot in portrait. Square images always qualify regardless, since they fit either way.

Both settings take effect on that frame's next refresh; there's nothing to save.

## 5. Steering which photos go where

Since there's no per-frame assignment, here are the levers you actually have when you care what shows on a given frame:

- **Orientation filtering.** This is the closest thing to targeting. Set a frame to Portrait and tick **Match orient.**, then upload the portrait photos you want it to favour. That frame will now only pull portrait images from the pool, while your landscape frames pull the rest. It's a coarse split by shape, not by subject, but for many setups (a tall frame in the hallway, wide frames in the lounge) it's exactly enough.

- **"Show next."** On the Images tab, each photo has a **Show next** button that jumps it to the front of the rotation. Be aware this is a **global** override, not frame-targeted: it forces that photo onto **whichever frame checks in next**, not a frame you choose. If you press Show next and then walk over and press the button on a specific frame to force it to refresh, that frame will be the one to pick it up — but the mechanism itself has no notion of "send to the hallway frame."

- **Curate the whole library.** Ultimately, the shared pool *is* the control surface. What's in the library is what every frame shows. If a photo shouldn't appear on any wall, delete it; if you want more of a certain subject in rotation everywhere, upload more of it.

If you genuinely need "these photos on that frame," that's a feature the current model doesn't support — it would require adding a per-frame assignment concept (playlists or groups) to the data model, the scheduler's eligibility filter, and the Screens UI.

## 6. Removing a frame

The trash icon on a screen's row removes that frame's record from the server. Use it to tidy up after retiring a frame, renaming one (the old name lingers as a stale row otherwise), or clearing out a test entry.

Removal only deletes the server-side record; it doesn't touch the frame itself. A frame that's still powered and on schedule will **reappear automatically** in the dashboard the next time it checks in, with a fresh request count. To make a removal stick, the frame has to actually be gone — unplugged, reflashed, or renamed.

---

For everything that's shared across all frames — the refresh schedule, dither settings, and the image library itself — see [manual.md](manual.md).
