# Installation

## What you need

**A computer to run the server on.** This can be anything on your local network — a Raspberry Pi, a spare laptop, a NAS, a desktop that's always on. A Raspberry Pi Zero 2 W is the most popular choice because it's cheap, silent, uses almost no power, and is more than fast enough. The server needs to be reachable by the frame at all times, so something that stays on makes more sense than a laptop you close. See [hardware.md](hardware.md) for the recommended Pi kit and where to buy.

**The frame.** The Hokku / Huessen 13.3" six-colour e-ink display. The board inside is an ESP32-S3 — the pre-built firmware is matched to it, you don't need to worry about the hardware details. See [hardware.md](hardware.md) for where to buy.

**A data-capable USB-C cable** for the initial setup. Not all USB-C cables carry data — charge-only cables are common and won't work. If nothing shows up when you plug in, try a different cable.

**2.4 GHz WiFi.** The frame's chip doesn't support 5 GHz. Make sure the network you want to use is 2.4 GHz (most routers broadcast both and you can use either SSID).

---

> **All commands in this guide assume you are in the project root directory** — the folder that contains `hokku_setup.bat`, `requirements.txt`, and the `tools/` and `webserver/` subdirectories. Open your terminal there before running anything.

## Contents

1. [Manual installation](#1-manual-installation) — no scripts, full control, any platform
   - [1.1 Install the image server](#11-install-the-image-server)
   - [1.2 Set up a Raspberry Pi (optional)](#12-set-up-a-raspberry-pi-optional)
   - [1.3 Flash and configure the frame](#13-flash-and-configure-the-frame)
2. [Using the setup wizard](#2-using-the-setup-wizard) — guided, downloads everything, Windows + Pi in one run
   - [2.1 Prerequisites](#21-prerequisites)
   - [2.2 Running the wizard](#22-running-the-wizard)
   - [2.3 What the wizard does, step by step](#23-what-the-wizard-does-step-by-step)
3. [Troubleshooting](#3-troubleshooting)

---

## 1. Manual Installation

You need two things: the **image server** running on a computer on your network, and the **firmware** flashed onto the frame over USB.

### 1.1 Install the image server

**Debian / Ubuntu (recommended)**

Download the `.deb` from the latest GitHub release, then:

```bash
apt install ./hokku-server_<version>_all.deb
```

The package installs a systemd service that starts automatically on boot. The web GUI will be at `http://<your-server>:8080/`. Photos go into `/var/lib/hokku/images/` — use the web uploader, or install Samba so you can drop files in from any machine on your network.

Useful service commands:

```bash
systemctl status hokku-server     # check it's running
systemctl restart hokku-server    # restart after manual config edits
journalctl -u hokku-server -f     # follow the logs
```

**Configuration**

The server takes the config file path as a command-line argument. The `.deb` install handles this automatically — the systemd service passes `/var/lib/hokku/config.json` and the file is created with sensible defaults on first start. Timezone follows the host OS — set it with `timedatectl set-timezone <IANA>` on the Pi.

A minimal config looks like this (the `version` field is required — without it the server replaces the file with a fresh default):

```json
{
  "version": 6,
  "refresh_image_at_time": ["0600", "1200", "1800"],
  "upload_dir": "/var/lib/hokku/images",
  "cache_dir": "/var/lib/hokku/cache",
  "port": 8080
}
```

All options can also be changed live from the web app without restarting the server.

**From source (any platform)**

Create a virtual environment, install dependencies, and start the server. The
server takes the path to a `config.json` as a required argument — on first start
it writes a fresh default there if the file doesn't exist yet:

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cd webserver
python -m hokku_server config.json
```

The `cd webserver` step is required — the server package lives there. The default
config points `upload_dir` at `/var/lib/hokku/images`; **on Windows or macOS edit
`config.json` and set `upload_dir` / `cache_dir` to real local paths** (e.g.
`D:/Hokku/images`), then create those folders — the server exits on start if they
don't exist. Web GUI at `http://<your-server>:8080/`.

**Windows: let the wizard do it.** On Windows the setup wizard automates this whole
path — creating the folders, writing a versioned `config.json`, and registering a
boot service — see [section 2](#2-using-the-setup-wizard), option *"This PC — install
the hokku server on this PC only"*.

---

### 1.2 Set up a Raspberry Pi (optional)

Skip this section if you're running the server on an existing machine. The setup wizard handles SD card imaging automatically on Windows — this section is for everyone else.

Use the official [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and configure these settings before writing:

- **OS:** Raspberry Pi OS Lite 64-bit (no desktop needed)
- **Hostname:** `hokku` (so it appears as `hokku.local` on the network)
- **WiFi:** your SSID and password, plus the correct country code
- **SSH:** enabled — strongly recommended, without it there's no way to check logs remotely
- **User:** create a user (e.g. `hokku` / `hokku`)
- **Timezone:** your local timezone

Once the Pi boots and you can SSH in, install the `.deb`:

```bash
scp hokku-server_*.deb hokku@hokku.local:~
ssh hokku@hokku.local
sudo apt install ./hokku-server_*.deb
```

The package creates a default config at `/var/lib/hokku/config.json` on first start and brings up the web UI at `http://hokku.local:8080/`.

**First boot is slow.** On a Pi Zero 2 W expect 3–8 minutes for the OS to fully initialise, install packages, and bring the webserver up. SSH in and tail `/var/log/hokku-firstboot-install.log` if you want to watch progress.

---

### 1.3 Flash and configure the frame

**Prerequisites**

- A data-capable USB-C cable — not all cables carry data, try a different one if nothing shows up
- A virtual environment with dependencies installed (if you haven't already):

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

**Step 1: Connect and identify the port**

Connect the frame to your computer via the USB-C charging port. Find the serial port:

- **Windows:** Device Manager → Ports (COM & LPT) — look for a Silicon Labs or CP210x device and note the COM number (e.g. `COM3`). If it doesn't appear, install the [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers).
- **Linux / macOS:** `ls /dev/ttyUSB* /dev/ttyACM* /dev/cu.usbserial*` — the frame typically appears as `/dev/ttyUSB0` or similar.

**Step 2: Flash the firmware**

Download `hokku-firmware_<tag>.bin` from the latest GitHub release, then:

```bash
esptool.py --chip esp32s3 --port <PORT> write_flash 0x0 hokku-firmware_<tag>.bin
```

The flash takes about 30 seconds.

**Step 3: Write the configuration**

The frame reads its configuration from NVS (non-volatile storage), written over USB. These are the values you need to supply:

| Field | Notes |
|---|---|
| WiFi SSID | 1–32 bytes, no quotes, backslashes, or newlines |
| WiFi Password | 8–63 characters, or empty for an open network |
| Secondary WiFi SSID | Optional. A fallback network the frame tries when the primary fails |
| Secondary WiFi Password | Required if a secondary SSID is set |
| WiFi Connection Order | Optional. `primary first` (default) or `last-used first` — try whichever network last worked. Only shown when a secondary SSID is configured |
| Server IP or hostname | The LAN IP or mDNS hostname of the machine running the image server (e.g. `192.168.1.10` or `hokku.local`) |
| Server Port | `8080` (default) |
| Screen Name | Optional, up to 64 bytes UTF-8, e.g. `Living Room` |

The setup tool can handle just the config-write step if you prefer not to do it by hand:

```bash
python tools/hokku_setup.py
# Select: ESP32: configure only (keep existing firmware)
```

**Step 4: Verify**

After rebooting with valid config the frame connects to WiFi, fetches its first image from the server, and settles into its refresh schedule. If something is wrong it renders a readable error message directly on the e-paper — no serial cable needed.

---

## 2. Using the Setup Wizard

The setup wizard (`hokku_setup.py` / `hokku_setup.bat`) is an interactive menu-driven tool that handles the full install in one run: imaging the Pi OS SD card, downloading firmware, and configuring and flashing the frame. It saves your settings between runs so you don't have to re-enter WiFi credentials every time.

On Windows, double-clicking `hokku_setup.bat` is enough — it creates a `.venv` in the project root, installs dependencies, and launches the wizard with the necessary privileges automatically.

### 2.1 Prerequisites

> If you followed the manual installation path in section 1 you already have these. Skip to [section 2.2](#22-running-the-wizard).

- Python 3.9+
- A virtual environment with dependencies (the `.bat` file does this for you on Windows):

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

- A data-capable USB-C cable connecting the frame to your computer

### 2.2 Running the wizard

```bash
python tools/hokku_setup.py
```

The wizard scans for a connected frame on startup and displays its current state:

```
  Connected frame
  ---------------
  Port:      COM3
  Firmware:  20260421035048Z  (up to date)
  Config:    WiFi=MyNetwork, server=192.168.1.10:8080, name=Living Room
```

It then presents a menu and pre-selects the most sensible option for the detected state:

```
  What would you like to do?
    [1] This PC — install the server here, then configure + flash ESP32
    [2] This PC — install the hokku server on this PC only
    [3] Raspberry Pi — image SD card, then configure + flash ESP32
    [4] Raspberry Pi — image SD card with hokku-server, skip ESP32
    [5] ESP32: configure + flash firmware  <-- default
    [6] ESP32: configure only (keep existing firmware)
    [7] ESP32: flash firmware only (keep existing config)
    [8] Advanced — install settings, cache management, uninstall
    [9] Exit
```

The default is chosen from the detected state — a connected frame that needs
configuring defaults to an ESP32 option; with no frame attached it defaults to
setting up the server. The **Raspberry Pi** options only appear on Windows (SD-card
imaging needs raw disk access); on Linux/macOS the menu shows the This-PC and ESP32
options only.

### 2.3 What the wizard does, step by step

**This PC [1] and [2] — run the server on the machine you're on (Windows)**

> These options require administrator privileges (registering a boot service and
> opening the firewall). `hokku_setup.bat` elevates automatically.

1. **Dependencies** — installs `requirements.txt` into the repo's `.venv` so the
   server can run.
2. **Install directory** — proposes a visible default (e.g. `D:\Hokku`) and creates
   `images/`, `cache/`, and `logs/` under it. Put photos in `images\` (drag-drop, or
   the web uploader); rendered panel data and thumbnails go in `cache\`.
3. **Config** — writes a versioned `config.json` pointing at those folders, plus your
   chosen mDNS hostname (default `hokku` → advertises as `hokku.local`) and port
   (default 8080).
4. **Firewall** — adds inbound allow rules for the HTTP port (TCP) and mDNS (UDP 5353),
   scoped to Private/Domain networks only.
5. **Boot service** — registers a Windows Scheduled Task that starts the server at
   boot as `SYSTEM` (no login needed), restarts it on failure, and logs to
   `logs\server.log`. It's started immediately, so you don't need to reboot.
6. **Address for the frame** — probes whether `hokku.local` resolves back to this PC
   and, if so, points the frame at `hokku.local` (survives DHCP changes); otherwise
   it falls back to this PC's detected LAN IP. Virtual/VPN adapters are filtered out
   and you can override the choice.
7. **Frame WiFi** — with option [1] it then flows into the ESP32 phase, pre-filling
   the WiFi and server address you just configured.

The Scheduled Task and firewall rules can be removed later via **Advanced → Uninstall
local server** (your photos and config are left in place). Note: a PC that sleeps is
unreachable when the frame wakes to refresh — the wizard warns and offers to disable
sleep-on-AC.

**Raspberry Pi [3] and [4] — Pi OS SD card imaging (Windows only)**

> These options require Windows and administrator privileges. They write directly to the raw disk.

1. **Select version** — the wizard lists installable `.deb` packages from two sources, newest first:
   - **Local builds** from `build/` (if you've built from source)
   - **GitHub releases** not already present locally

   ```
     Available versions:
       1. hokku-server_3.0.0~alpha2-1_all.deb  [2026-05-12, local build]
       2. hokku-server_3.0.0~alpha2-1_all.deb  [2026-05-12, GitHub v3.0.0-alpha2]
       3. hokku-server_3.0.0~alpha1-30_all.deb  [2026-04-28, GitHub v3.0.0-alpha1]

     Select version [1-3]:
   ```

2. **Select SD card** — the wizard scans for removable drives sized 2–256 GB and suggests the most likely candidate. You can confirm, wait for a different card to be inserted, or pick from a list of all drives manually.

3. **Download Pi OS** — if no image is cached, the wizard downloads the latest Pi OS Lite 64-bit image (~550 MB compressed) from `downloads.raspberrypi.com` and caches it for future runs.

4. **Configure** — you're prompted for:
   - WiFi SSID and password (saved to `.cache/settings.json` for next run)
   - Linux username and password (defaults: `hokku` / `hokku`)
   - SSH enabled? (strongly recommended)
   - Samba (Windows file share) installed? (uses the same credentials)
   - Bonjour/mDNS — whether to advertise the server on the network and under what hostname (e.g. `hokku.local`). If a hostname is given, the wizard checks whether it is already in use on the network before proceeding.
   - WiFi country code (ISO 3166, e.g. `GB`, `US`, `NL`)
   - Timezone (IANA, e.g. `Europe/London`, `America/Chicago`)

5. **Write** — after you type `YES` to confirm, the image is decompressed on the fly and written to the SD card (~3 GB, takes 3–10 minutes). First-boot scripts are injected into the card's boot partition to configure the OS, copy the `.deb`, and run the install automatically on first boot.

6. **Wait for the Pi** — two distinct phases, each with a live progress bar showing elapsed and remaining time:

   **Phase 1 — OS setup (Boot 1, up to 5 min).** The Pi runs `firstrun.sh`, which sets the hostname, configures WiFi via NetworkManager, creates the user, and enables SSH. It then announces itself on the network as `hokku-installing.local` via avahi and reboots. The wizard polls for this hostname to appear:

   ```
     Phase 1/2: waiting for hokku-installing.local (Boot 1 — OS setup, ~1-2 min, up to 5 min)

     [████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░]  1:06 elapsed  3:54 remaining

     hokku-installing.local is up at 192.168.x.x after 1:06.

     Boot 2 (package install) is now running.
     You can SSH in to watch progress:
       ssh hokku@hokku-installing.local
       tail -f /var/log/hokku-firstboot-install.log
   ```

   When `hokku-installing.local` resolves, the Pi's IP is known. If SSH was enabled you can log in immediately to watch the install log in real time. The `hokku-installing.local` name stays up for the entire duration of Boot 2 so you don't lose access mid-install.

   **Phase 2 — package install (Boot 2, up to 15 min).** After the reboot, `firstboot-install.sh` runs: it calls `apt update`, installs the hokku-server `.deb` (pulling in ~90 Debian packages), and optionally installs Samba. The wizard probes the webserver **directly by IP** — not by hostname — to guarantee it is talking to the right machine even if another server is already using the configured `.local` name on the network:

   ```
     Phase 2/2: waiting for webserver at 192.168.x.x (Boot 2 — package install)

     Polling http://192.168.x.x:8080/hokku/api/status

     [████████████████████████░░░░░░░░░░░░]  5:25 elapsed  9:35 remaining

     Webserver up after 5:25.
   ```

   The probe checks for a valid JSON response with a `server_time` field to confirm it is a genuine hokku server, not a different service that happens to return HTTP 200.

   Once the webserver is confirmed up, `firstboot-install.sh` stops and disables avahi — `hokku-installing.local` disappears. The wizard then applies the requested Bonjour hostname via the server's config API (if it differs from the default `hokku`), and the server's own zeroconf library takes over advertising under the configured name:

   ```
     Bonjour configured: myhokku.local (192.168.x.x)

     Server ready at http://myhokku.local:8080/ (IP 192.168.x.x).
   ```

   If either phase times out, the wizard asks `Keep waiting? [Y/n]` so you can extend the wait without restarting.

**ESP32: configure + flash / configure only / flash only — ESP32 frame**

1. **Device detection** — the wizard scans USB serial ports for an ESP32-S3. If multiple devices are found you'll be asked to pick one. The device's current firmware version and config are displayed.

2. **Configuration prompts** (the configure options) — you're asked for WiFi SSID, WiFi password, server IP, server port (default `8080`), and an optional screen name. You can also configure an optional secondary WiFi network; if you do, a connection-order prompt follows (`primary first` or `last-used first`). The wizard checks the server is reachable before writing; if it isn't you'll see a warning and can continue anyway.

3. **Firmware download and flash** (options 3 and 5) — the wizard fetches the latest `hokku-firmware_*.bin` from GitHub releases (or imports a local build if one exists) and flashes it over USB. Takes about 30 seconds. A boot check follows: the wizard reads serial output for 10 seconds and reports whether the firmware started cleanly.

![Setup tool configuring a frame](../images/configurator.png)

**Advanced [6]**

- **Show / edit install settings** — view or change the cached WiFi, user, SSH, country, timezone values. Passwords can be revealed on request.
- **Download everything into .cache** — prefetch the Pi OS image, hokku-server `.deb`, and firmware so a subsequent install can run fully offline.
- **Clear .cache** — frees disk space; the next run re-downloads anything it needs.

---

## 3. Troubleshooting

**`esptool not installed`** — activate your `.venv` and run `pip install -r requirements.txt` from the project root.

**`No serial devices found`** — the cable is charge-only, or the driver isn't installed. On Windows, install the [CP210x driver](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) if the port doesn't appear in Device Manager.

**Server IP warning during configure** — the wizard checks the server is reachable before writing config. If your server isn't running yet, proceed anyway; the frame will retry on its own schedule once the server is up.

**Pi SD card imaging requires Windows and admin** — on other platforms use the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) manually and follow [section 1.2](#12-set-up-a-raspberry-pi-optional).

**First boot very slow** — normal. The Pi installs ~90 Debian packages on first boot and waits for NTP clock sync before running `apt update`. On slow SD cards or a congested network this can push past 8 minutes. If SSH is enabled, log in via `hokku-installing.local` while Phase 1 is still running and tail `/var/log/hokku-firstboot-install.log` to watch progress.

**Frame shows an error on the e-paper** — the firmware renders configuration and connectivity errors directly on screen. Read the message and fix the relevant setting (wrong WiFi password, wrong server IP, etc.), then re-run configure.

**Boot check reports unknown** — the frame may have entered deep sleep immediately after flashing. This is normal; it doesn't mean the flash failed. Check the web app to see if the frame checks in on its next scheduled refresh.
