#pragma once

#include <stdbool.h>

/* Minimal READ-ONLY driver for the SY6974B charger (I2C 0x6b).
 *
 * Deliberately has NO register-write path — charge current / voltage /
 * enable are hardware defaults and must never be touched by firmware
 * (LiPo mis-charge risk; the reference E1003 project ships read-only for
 * the same reason). This replaces the Spectra-6 board's USB_DETECT GPIO:
 * the E1003 has no VBUS logic pin (native-USB pins are I2C), so USB/charge
 * presence is read from the charger's status register instead. */

/* Bring up I2C0 (SDA 19 / SCL 20) and probe the charger. Safe to call
 * once at boot; returns false if the charger doesn't ACK (all subsequent
 * queries then report "no USB, not charging"). */
bool charger_init(void);

bool charger_available(void);

/* One status-register read (REG08). Either out-pointer may be NULL.
 *   usb_present — PG_STAT: valid input power (USB or adapter) attached.
 *   charging    — CHRG_STAT is pre-charge or fast-charge (not done/idle).
 * Returns false (outputs untouched) if the charger is absent or the read
 * fails — callers treat that as "on battery". */
bool charger_read_status(bool *usb_present, bool *charging);
