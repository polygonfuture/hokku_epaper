#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* IT8951 e-paper controller driver for the reTerminal E1003
 * (ED103TC2 panel, 16-level grayscale).
 *
 * The panel is NATIVE LANDSCAPE: 1872 wide × 1404 high — GET_DEV_INFO
 * reports exactly that (verified on hardware 2026-07-18: 1872x1404,
 * imgbuf 0x003dbe78). Marketing specs quote the portrait "1404×1872";
 * the controller does not. Portrait hanging is the server's job (it
 * rotates into the native-landscape buffer, same as the Spectra-6 frame).
 *
 * Framebuffer format: 4 bits per pixel, 2 px/byte, HIGH nibble = leftmost
 * pixel of the pair, 0x0 = black … 0xF = white, native-landscape rows
 * (1872 px = 936 bytes/row, 1404 rows), left→right top→bottom.
 * (The driver handles the panel's right-to-left row scan internally.)
 *
 * Hardware-safety contract (see ../README.md — binding):
 *   - Power rails sequenced: core (+50 ms) → bias (+10 ms) up; reverse down.
 *   - GET_DEV_INFO is validated (1404×1872) before anything else is driven —
 *     doubles as the anti-mis-flash gate. On mismatch the driver refuses to
 *     operate.
 *   - VCOM is READ-ONLY: read + logged at init, never written. An implausible
 *     read aborts init (comms failure), it does NOT trigger a fallback write.
 *   - Every power-down path waits for the display engine (LUTAFSR == 0),
 *     sends SLEEP, drives the SPI signal lines low, then drops bias before
 *     core. Never cut rails mid-waveform.
 */

#define IT8951_PANEL_W       1872   /* native landscape — controller-reported */
#define IT8951_PANEL_H       1404
#define IT8951_FRAME_BYTES   (IT8951_PANEL_W * IT8951_PANEL_H / 2)  /* 1,314,144 */

/* Power up rails, init the controller, validate the panel, read VCOM.
 * Idempotent — returns immediately if already initialised.
 * Returns ESP_OK only when GET_DEV_INFO validated 1404×1872 AND the VCOM
 * read-back was plausible. On any failure the panel is powered back down. */
esp_err_t it8951_init(void);

/* True once it8951_init() has fully succeeded (panel validated). */
bool it8951_panel_ok(void);

/* VCOM read back at init, in millivolts (e.g. 1400 = −1.40 V). 0 = not read. */
uint16_t it8951_vcom_mv(void);

/* Load a full 4bpp frame and refresh: INIT (anti-ghosting white clear)
 * followed by GC16. Calls it8951_init() lazily. Blocks until the refresh
 * completes (tens of seconds). Returns ESP_OK on success. */
esp_err_t it8951_display_full(const uint8_t *fb);

/* Contract-compliant power-down: LUTAFSR wait → SLEEP → SPI teardown →
 * signal lines low → bias off → core off. Safe to call in any state
 * (idempotent; also safe if init failed or never ran). Call before deep
 * sleep and before esp_restart(). */
void it8951_power_off(void);
