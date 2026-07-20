# Hokku firmware — Seeed reTerminal E1003 (monochrome 16-gray)

Port of the Hokku Spectra-6 frame firmware (`../firmware/`) to the
**Seeed reTerminal E1003**: ESP32-S3R8, 10.3″ ED103TC2 panel (1404×1872,
16-level grayscale), ITE IT8951 controller.

The application stack — state machine (USB_AWAKE / BATTERY_IDLE /
DEEP_SLEEP / REFRESH), WiFi with BSSID fast-reconnect, the Hokku HTTP
protocol (`X-Screen-Name`, `X-Frame-State`, `X-Sleep-Seconds`,
`X-Server-Time-Epoch`, RTC log-ring upload), epoch-anchored deep sleep and
the NVS config schema — is carried over from the Spectra-6 firmware
near-verbatim. The hardware layer is new: `it8951.c` (display),
`charger_sy6974b.c` (read-only charger status), `pins.h` (board map).

**Wire format:** the server must send exactly **1,314,144 bytes** —
1404×1872 at 4 bpp, 2 px/byte, high nibble = left pixel, `0x0` black …
`0xF` white, single buffer (no dual-half split). The firmware requests it
with the header `X-Panel-Type: mono16_e1003`.

**Config:** identical NVS schema + namespace as the Spectra-6 firmware, so
the existing `hokku-setup` provisioning tools work unchanged over the
E1003's USB-C (CH340K) serial port.

---

## ⚠ Never cross-flash

**Never flash the Spectra-6 build (`hokku_epaper.bin`) onto the E1003**, or
this build onto the Spectra-6 frame. The Spectra-6 pin map drives the
E1003's KEY0 button (GPIO3) push-pull — pressing the button then shorts a
driven output to GND — and fights the IT8951's HRDY output on GPIO13.
Guards in place: distinct binary name (`hokku_epaper_e1003.bin`), and the
driver validates `GET_DEV_INFO` (1404×1872) before driving anything.
Label your boards.

## Hardware-safety contract (binding — do not "simplify" these away)

E-ink is damaged by DC bias: interrupted waveforms and wrong VCOM, not
ordinary use.

1. **Path-independent power-down.** Every exit (success, error, timeout,
   low battery, restart) goes through `it8951_power_off()`:
   display-engine idle → `SLEEP` → SPI signal lines driven low → bias
   (GPIO11) off → core (GPIO21) off. Never cut rails mid-waveform; never
   skip the sequencing.
2. **Low-battery gate.** `perform_refresh` refuses to start a GC16 below
   `BATT_LOW_MV` without input power (brownout mid-refresh = hazard #1).
3. **VCOM is read-only.** Read + logged + sanity-checked at init; an
   implausible read aborts (comms failure) — it never triggers a write.
   The factory value (~1400 mV) is stored in the module. A manual one-time
   set exists behind `-DE1003_ALLOW_VCOM_WRITE` (selector `0x0002` — this
   panel silently ignores `0x0001`) for a provably uncalibrated module only.
4. **Charger is read-only.** `charger_sy6974b.c` has no register-write
   path — never add one (LiPo mis-charge risk).
5. **Wake sources: KEY0 + timer only.** GPIO0 (BOOT / probable `SY_INT`)
   stays out of the ext1 mask until bench-traced (stuck-low at reset =
   ROM download mode).
6. **SPI stays at 4 MHz** until a faster clock is re-validated with
   register read-backs + the M1 ramp (reads garble above ~4 MHz on this
   hardware; a corrupted register write is a hardware risk).

## Building

ESP-IDF **5.5.x**, target `esp32s3`:

```
idf.py set-target esp32s3
idf.py build
idf.py -p <COM-port> flash monitor
```

No IDF installed? The Espressif Docker image works:

```
docker run --rm -it -v .:/project -w /project espressif/idf:v5.5.1 idf.py build
```

**Flashing/console go through the onboard CH340K → UART0** (the S3's
native USB is not wired). The CH340K's DTR/RTS auto-reset works with
esptool and also hard-resets the chip out of deep sleep — no wake-button
dance needed to reflash.

## Bring-up milestones

- **M0 — skeleton:** builds, flashes over the CH340K COM port, boots,
  joins WiFi, logs on UART0. (Display never powered.)
- **M1 — panel bring-up (no server):** set `M1_RAMP_TEST 1` in `main.c`.
  Boot → gray ramp. Gate: `GET_DEV_INFO` logs `1404x1872`; 16 clean
  monotonic bands, **black left → white right**; **marker square top-left**
  (top-right ⇒ row-mirroring wrong); VCOM read-back logged **≈1400 mV**;
  note the logged INIT/GC16 refresh times and keep `DISPLAY_TIMEOUT_MS`
  comfortably above them.
- **M2 — real photo:** server-side `mono16_e1003` wire format (minimal
  hook), then normal operation with `M1_RAMP_TEST 0`.
- **M3 — power:** full battery cycle; measure deep-sleep µA (expect µA-range
  with both EPD rails gated + holds; if high, check GPIO39/40/11 holds).

## Bench-verify list (one-time, per the schematic review)

- Which physical button is KEY0/GPIO3 (assumed the top "Refresh" button).
- `USER_LED` polarity (`pins.h`: aitjcize says 1=on, Zephyr DTS says
  active-low — flip `USER_LED_ON` if inverted).
- First M1 boot: confirm the VCOM log line reads ~1400 mV.
