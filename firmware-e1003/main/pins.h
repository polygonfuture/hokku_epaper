#pragma once

/* reTerminal E1003 GPIO map.
 *
 * Source of truth: the E1003 KiCad schematic (202004522_reTerminal_E1003_V1.0,
 * net labels read off the ESP32-S3 symbol), cross-checked against the Zephyr
 * board port, Seeed's Arduino cookbook, and aitjcize/esp32-photoframe's E1003
 * board header. Every value below matched across all sources.
 *
 * NOTE: this map shares ZERO pins with the Spectra-6 Hokku frame firmware.
 * Never flash that build onto this board (or this build onto the frame) —
 * see README.md "Never cross-flash".
 */

/* ── E-paper / IT8951 (SPI2, shared with microSD) ─────────────────── */
#define PIN_EPD_SCLK        7   /* ITE_SD_SCK  — shared bus clock            */
#define PIN_EPD_MISO        8   /* ITE_SD_MISO — IT8951 reads REQUIRE MISO   */
#define PIN_EPD_MOSI        9   /* ITE_SD_MOSI                               */
#define PIN_EPD_CS         10   /* ITE_CS#  — software CS (must stay low
                                 * across the HRDY gap inside a transaction) */
#define PIN_EPD_RST        12   /* ITE_RST# — active low                     */
#define PIN_EPD_HRDY       13   /* ITE_BUSY# — HIGH = ready, LOW = busy      */

/* Two-stage EPD power. Bring-up order: core first (+50 ms), then bias
 * (+10 ms). Teardown order: bias off first, core off last. See it8951.c. */
#define PIN_ITE_VCC_EN     21   /* IT8951 core 3V3/1V8 rails (active high)   */
#define PIN_EPD_DRIVE_EN   11   /* Load switch U22 → TPS651851 EPD bias PMIC
                                 * (VGH/VGL/VPOS/VNEG/VCOM) (active high)    */

/* ── microSD (same SPI bus — unused by this firmware, but must be
 *    parked safely so it can never fight the IT8951 on MISO) ────────── */
#define PIN_SD_CS          14   /* drive HIGH while bus is active            */
#define PIN_SD_DET         15
#define PIN_SD_EN          39   /* SD power rail (active high) — kept LOW    */

/* ── Buttons (each: 10K pull-up to 3V3, button shorts pin to GND) ──── */
#define PIN_KEY0            3   /* "refresh" per Zephyr; RTC-wake source.
                                 * GPIO3 is a strapping pin (JTAG-sel): fine
                                 * as a button, don't hold it during reset.  */
#define PIN_KEY1            4
#define PIN_KEY2            5
/* GPIO0 = BOOT button, and (per the schematic's ESP_IO/SY_INT net) most
 * likely also the SY6974B charger IRQ. NOT used as a wake source until a
 * bench trace confirms the charger can't hold it low across a reset
 * (GPIO0 low at reset = ROM download mode). */

/* ── LED ─────────────────────────────────────────────────────────────
 * USER_LED polarity: ACTIVE-LOW on this unit (per the Zephyr DTS; aitjcize's
 * "non-inverted 1=on" was wrong for the green LED). Bench-confirmed: with the
 * old ON=1/OFF=0 the firmware's "off" (level 0) drove the LED constantly ON.
 * So ON drives the pin LOW, OFF drives it HIGH. */
#define PIN_USER_LED       16
#define USER_LED_ON         0
#define USER_LED_OFF        1

/* ── Battery ─────────────────────────────────────────────────────────
 * VBAT — R19/R21 10K/10K divider (×2.0) — ADC1_CH0 on GPIO1, gated by a
 * FET on VBAT_EN. Assert VBAT_EN only around the measurement: left high
 * it burns ~200 µA through the divider forever. */
#define PIN_VBAT_ADC        1   /* ADC1_CH0 */
#define PIN_VBAT_EN        40   /* active high, non-RTC → gpio_hold in sleep */
#define VBAT_DIVIDER_MULT   2.0f

/* ── I2C0 (shared: SY6974B charger 0x6b, PCF8563 RTC 0x51,
 *          SHT4x temp 0x44, GT911 touch 0x5d) ──────────────────────── */
#define PIN_I2C_SDA        19
#define PIN_I2C_SCL        20

/* ── Capacitive touch (GT911) — UNUSED by this firmware ──────────────
 * We never read touch, but the GT911's reset line must be driven or the
 * controller stays active and burns ~5 mA in deep sleep. The community
 * ESPHome port (ar0v3r/reTerminal-E1003-ESPHome) found GPIO48 floating
 * HIGH is the single biggest sleep-current culprit alongside the SD rail.
 * RSTN is active-low, so we hold it LOW (chip parked in reset = lowest
 * power) at init and through deep sleep. GPIO48 is non-RTC → gpio_hold in
 * sleep, exactly like SD_EN/VBAT_EN. */
#define PIN_TOUCH_RES      48   /* GT911 RSTN — held LOW (reset/low-power) */
#define PIN_TOUCH_INT       2   /* GT911 INT — input, left isolated        */

/* ── Console ─────────────────────────────────────────────────────────
 * UART0 (GPIO43/44) → onboard CH340K → USB-C. Native USB-Serial-JTAG is
 * unavailable (GPIO19/20 are I2C). esptool auto-reset works via the
 * CH340K's DTR#/RTS# into CHIP_PU/GPIO0 — and conveniently also hard-
 * resets the chip out of deep sleep, so reflash never needs a USB-wake
 * source the way the Spectra-6 board did. */
