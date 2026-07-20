/* IT8951 driver — reTerminal E1003 (ED103TC2 1404×1872, 16-gray).
 *
 * Ported for the Hokku E1003 firmware from four cross-checked references:
 * Waveshare EPD_IT8951.c (canonical C command set), Seeed GxEPD2_ED103TC2 +
 * Seeed_GFX Tcon.cpp (this exact panel), ESPHome it8951, and — most load-
 * bearing — aitjcize/esp32-photoframe's driver_it8951.c, a working ESP-IDF
 * photo frame on this same board, whose empirically-proven choices we copy:
 *
 *   - Single 4 MHz SPI clock for everything. Reads above ~4 MHz return stale
 *     MISO on this hardware (garbled GetSystemInfo). Do NOT raise this
 *     without re-validating the M1 gray ramp + register read-backs.
 *   - Software CS: the IT8951 requires CS to stay LOW across the HRDY gap
 *     between the 16-bit preamble word and the payload, which hardware CS
 *     can't do.
 *   - Native 4bpp load, little-endian selector, high nibble = first pixel.
 *   - Each row is streamed in REVERSED 16-bit-word order — the ED103TC2
 *     scans rows right-to-left. (Verify orientation with the M1 ramp; the
 *     ramp deliberately carries a corner marker.)
 *   - The whole CS-low image stream holds the SPI bus exclusively
 *     (spi_device_acquire_bus): the bus is shared with the microSD, and an
 *     SD transaction sneaking between row transmits while CS is low would
 *     desync the image load.
 *   - FORCE_TEMP before updates — without it GC16 can refresh blank.
 *   - Full INIT (white) clear before every GC16 — GC16 alone ghosts on this
 *     panel.
 *
 * VCOM policy (differs from every reference — deliberate, see README):
 * read-only. The factory value is stored in the module (stock firmware's
 * Tcon layer plants 1400 mV once via selector 0x0002 and never re-writes on
 * wake). We read + log + sanity-check it and refuse to run on an implausible
 * read; we never write. A manual one-time set exists behind
 * E1003_ALLOW_VCOM_WRITE for a provably uncalibrated module only.
 */

#include "it8951.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "pins.h"

static const char *TAG = "it8951";

/* ── Command / register set (Waveshare header names, cross-checked) ── */
#define TCON_SYS_RUN        0x0001
#define TCON_SLEEP          0x0003
#define TCON_REG_RD         0x0010
#define TCON_REG_WR         0x0011
#define TCON_LD_IMG_AREA    0x0021
#define TCON_LD_IMG_END     0x0022
#define CMD_DPY_AREA        0x0034
#define CMD_VCOM            0x0039
#define CMD_FORCE_TEMP      0x0040
#define CMD_GET_DEV_INFO    0x0302

#define REG_I80CPCR         0x0004
#define REG_LISAR           0x0208  /* image buffer addr: low16 here, high16 at +2 */
#define REG_LUTAFSR         0x1224  /* display engine busy: nonzero = busy */

/* SPI preamble words — every packet starts with one (the IT8951 has no D/C
 * line). Read packets additionally clock one dummy word before real data. */
#define PRE_CMD             0x6000
#define PRE_WRITE           0x0000
#define PRE_READ            0x1000

/* Load-image config word fields */
#define LD_LITTLE_ENDIAN    0
#define LD_4BPP             2
#define LD_ROTATE_0         0

/* Display modes (ED103TC2) */
#define MODE_INIT           0   /* full white clear */
#define MODE_GC16           2   /* 16-level grayscale */

/* Clocks / timeouts. 4 MHz is the reference-proven ceiling (see header).
 * DISPLAY_TIMEOUT starts generous; M1 logs the measured refresh time —
 * keep the timeout comfortably above the measured worst case. */
#define SPI_CLOCK_HZ        (4 * 1000 * 1000)
#define HRDY_TIMEOUT_MS     5000
#define DISPLAY_TIMEOUT_MS  60000

#define ROW_BYTES           (IT8951_PANEL_W / 2)   /* 702 */
#define ROW_WORDS           (ROW_BYTES / 2)        /* 351 */

typedef enum {
    PWR_OFF = 0,        /* rails down, SPI free */
    PWR_ON,             /* rails up, SPI up, controller may or may not be OK */
    PWR_VALIDATED,      /* GET_DEV_INFO passed, VCOM plausible — usable */
} pwr_state_t;

static pwr_state_t          s_state = PWR_OFF;
static spi_device_handle_t  s_spi   = NULL;
static uint8_t             *s_rowbuf = NULL;       /* internal-RAM DMA bounce */
static uint32_t             s_img_buf_addr = 0;
static uint16_t             s_vcom_mv = 0;

/* ── Low-level SPI (16-bit words, MSB first, software CS) ──────────── */

static inline void cs_low(void)  { gpio_set_level(PIN_EPD_CS, 0); }
static inline void cs_high(void) { gpio_set_level(PIN_EPD_CS, 1); }

static bool wait_hrdy(uint32_t timeout_ms)
{
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (gpio_get_level(PIN_EPD_HRDY) == 0) {
        if (esp_timer_get_time() > deadline) {
            ESP_LOGE(TAG, "HRDY timeout (%lu ms)", (unsigned long)timeout_ms);
            return false;
        }
        vTaskDelay(1);
    }
    return true;
}

static void spi_tx16(uint16_t v)
{
    uint8_t b[2] = { (uint8_t)(v >> 8), (uint8_t)v };
    spi_transaction_t t = { .length = 16, .tx_buffer = b };
    spi_device_polling_transmit(s_spi, &t);
}

static uint16_t spi_rx16(void)
{
    uint8_t tx[2] = { 0, 0 }, rx[2] = { 0, 0 };
    spi_transaction_t t = { .length = 16, .rxlength = 16,
                            .tx_buffer = tx, .rx_buffer = rx };
    spi_device_polling_transmit(s_spi, &t);
    return ((uint16_t)rx[0] << 8) | rx[1];   /* words arrive MSB-first */
}

static bool write_cmd(uint16_t cmd)
{
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) return false;
    cs_low();
    spi_tx16(PRE_CMD);
    bool ok = wait_hrdy(HRDY_TIMEOUT_MS);   /* CS stays low across the gap */
    if (ok) spi_tx16(cmd);
    cs_high();
    return ok;
}

static bool write_data16(uint16_t v)
{
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) return false;
    cs_low();
    spi_tx16(PRE_WRITE);
    bool ok = wait_hrdy(HRDY_TIMEOUT_MS);
    if (ok) spi_tx16(v);
    cs_high();
    return ok;
}

/* Read n data words: 0x1000 preamble, one dummy word discarded, then data.
 * CS held low for the whole packet; HRDY re-checked before every word. */
static bool read_words(uint16_t *out, int n)
{
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) return false;
    cs_low();
    spi_tx16(PRE_READ);
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) { cs_high(); return false; }
    (void)spi_rx16();                        /* dummy word */
    for (int i = 0; i < n; i++) {
        if (!wait_hrdy(HRDY_TIMEOUT_MS)) { cs_high(); return false; }
        out[i] = spi_rx16();
    }
    cs_high();
    return true;
}

static bool reg_write(uint16_t addr, uint16_t val)
{
    return write_cmd(TCON_REG_WR) && write_data16(addr) && write_data16(val);
}

static bool reg_read(uint16_t addr, uint16_t *out)
{
    return write_cmd(TCON_REG_RD) && write_data16(addr) && read_words(out, 1);
}

/* ── Mid-level ops ──────────────────────────────────────────────────── */

/* Poll the display engine until idle. Returns false on timeout (caller
 * decides how to proceed — power_off still sequences safely either way). */
static bool wait_display_idle(uint32_t timeout_ms)
{
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (true) {
        uint16_t v = 1;
        if (!reg_read(REG_LUTAFSR, &v)) return false;
        if (v == 0) return true;
        if (esp_timer_get_time() > deadline) {
            ESP_LOGE(TAG, "display engine (LUTAFSR) timeout after %lu ms",
                     (unsigned long)timeout_ms);
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* GET_DEV_INFO: 20 words. Validates panel geometry — this is also the
 * anti-mis-flash gate: wrong board / dead comms → we refuse to drive. */
static bool read_and_validate_dev_info(void)
{
    for (int attempt = 1; attempt <= 5; attempt++) {
        uint16_t info[20] = { 0 };
        if (write_cmd(CMD_GET_DEV_INFO) && read_words(info, 20)) {
            uint16_t w = info[0], h = info[1];
            uint32_t addr = ((uint32_t)info[3] << 16) | info[2];
            if (w == IT8951_PANEL_W && h == IT8951_PANEL_H && addr != 0) {
                s_img_buf_addr = addr;
                ESP_LOGI(TAG, "panel %ux%u, imgbuf=0x%08lx, FW '%.16s' LUT '%.16s'",
                         w, h, (unsigned long)addr,
                         (const char *)&info[4], (const char *)&info[12]);
                return true;
            }
            ESP_LOGW(TAG, "dev info mismatch (attempt %d): %ux%u addr=0x%08lx",
                     attempt, w, h, (unsigned long)addr);
        } else {
            ESP_LOGW(TAG, "dev info read failed (attempt %d)", attempt);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    return false;
}

/* VCOM — READ-ONLY (see file header + README). */
static bool read_vcom(void)
{
    uint16_t v = 0;
    if (!write_cmd(CMD_VCOM) || !write_data16(0x0000) || !read_words(&v, 1)) {
        ESP_LOGE(TAG, "VCOM read failed (comms)");
        return false;
    }
    if (v == 0 || v > 5000) {
        /* Implausible. Treated as a COMMS FAILURE, not as "module needs a
         * VCOM" — flaky SPI is overwhelmingly the more likely cause, and a
         * write here could permanently replace good factory calibration.
         * If M1 bench work proves the module genuinely has no stored VCOM
         * (read is stable-invalid across boots AND grayscale is broken),
         * use the manual one-time set below. */
        ESP_LOGE(TAG, "VCOM read-back implausible (%u mV) — refusing to run. "
                      "Expected ~1400 (-1.40 V, stock value for the ED103TC2).", v);
        return false;
    }
    s_vcom_mv = v;
    ESP_LOGI(TAG, "VCOM (factory, read-only): -%u.%02u V (%u mV)",
             v / 1000, (v % 1000) / 10, v);
    return true;
}

#ifdef E1003_ALLOW_VCOM_WRITE
/* Manual one-time diagnostic VCOM set. NEVER called by firmware logic —
 * compile with -DE1003_ALLOW_VCOM_WRITE and invoke by hand from app_main
 * for a provably uncalibrated module only. Selector 0x0002 is the only one
 * this panel honors (0x0001 is silently ignored by the E1003 panel
 * firmware — ESPHome's E1003 preset documents this). */
void it8951_vcom_set_once(uint16_t mv)
{
    ESP_LOGW(TAG, "MANUAL VCOM WRITE: %u mV (selector 0x0002)", mv);
    write_cmd(CMD_VCOM);
    write_data16(0x0002);
    write_data16(mv);
    uint16_t back = 0;
    write_cmd(CMD_VCOM); write_data16(0x0000); read_words(&back, 1);
    ESP_LOGW(TAG, "VCOM read-back after write: %u mV", back);
}
#endif

/* Force panel temperature so the IT8951 selects a usable waveform. Without
 * this, GC16 can refresh blank (reference + Seeed both force it). A fixed
 * room-temperature value is sufficient — waveform tolerance is wide. */
static bool force_temp(void)
{
    return write_cmd(CMD_FORCE_TEMP) && write_data16(1) && write_data16(20);
}

/* ── Rails / GPIO / SPI lifecycle ───────────────────────────────────── */

static void epd_gpio_setup(void)
{
    gpio_config_t out = {
        .pin_bit_mask = (1ULL << PIN_EPD_CS) | (1ULL << PIN_EPD_RST) |
                        (1ULL << PIN_ITE_VCC_EN) | (1ULL << PIN_EPD_DRIVE_EN) |
                        (1ULL << PIN_SD_CS) | (1ULL << PIN_SD_EN),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&out);
    gpio_config_t in = {
        .pin_bit_mask = (1ULL << PIN_EPD_HRDY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,   /* board has one; belt-and-braces */
    };
    gpio_config(&in);
}

static esp_err_t spi_setup(void)
{
    spi_bus_config_t bus = {
        .mosi_io_num = PIN_EPD_MOSI,
        .miso_io_num = PIN_EPD_MISO,        /* full duplex — IT8951 reads */
        .sclk_io_num = PIN_EPD_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) { ESP_LOGE(TAG, "spi_bus_initialize: %s", esp_err_to_name(err)); return err; }

    spi_device_interface_config_t dev = {
        .mode = 0,
        .clock_speed_hz = SPI_CLOCK_HZ,
        .spics_io_num = -1,                 /* software CS (see file header) */
        .queue_size = 1,
    };
    err = spi_bus_add_device(SPI2_HOST, &dev, &s_spi);
    if (err != ESP_OK) { ESP_LOGE(TAG, "spi_bus_add_device: %s", esp_err_to_name(err)); }
    return err;
}

static void spi_teardown(void)
{
    if (s_spi) {
        spi_bus_remove_device(s_spi);
        spi_bus_free(SPI2_HOST);
        s_spi = NULL;
    }
}

esp_err_t it8951_init(void)
{
    if (s_state == PWR_VALIDATED) return ESP_OK;

    /* Bounce buffer: SPI-master DMA cannot transmit from PSRAM on the S3,
     * so rows are copied through internal RAM. Allocated once, kept. */
    if (!s_rowbuf) {
        s_rowbuf = heap_caps_malloc(ROW_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_rowbuf) { ESP_LOGE(TAG, "rowbuf alloc failed"); return ESP_ERR_NO_MEM; }
    }

    epd_gpio_setup();

    /* Rails up, IN ORDER: core first, settle, then bias. Never drive the
     * SPI/RST/CS lines into an unpowered controller (ESD-diode backpower). */
    cs_high();
    gpio_set_level(PIN_EPD_RST, 1);
    gpio_set_level(PIN_ITE_VCC_EN, 1);          /* IT8951 core 3V3/1V8 */
    vTaskDelay(pdMS_TO_TICKS(50));              /* let core rails settle */

    /* The SPI bus is shared with the microSD. Park its CS high (deselected)
     * BEFORE the bus goes live, and power its rail while the bus is active:
     * an unpowered card on a driven bus would be parasitically back-fed
     * through CLK/MOSI. The card is never mounted — this is purely to keep
     * the shared bus electrically clean; the rail drops again in power_off. */
    gpio_set_level(PIN_SD_CS, 1);
    gpio_set_level(PIN_SD_EN, 1);
    vTaskDelay(pdMS_TO_TICKS(10));

    esp_err_t err = spi_setup();
    if (err != ESP_OK) { it8951_power_off(); return err; }

    gpio_set_level(PIN_EPD_DRIVE_EN, 1);        /* TPS651851 bias PMIC */
    vTaskDelay(pdMS_TO_TICKS(10));
    s_state = PWR_ON;

    /* Reset pulse, then wait for the controller to come ready. */
    gpio_set_level(PIN_EPD_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(PIN_EPD_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) { it8951_power_off(); return ESP_FAIL; }

    if (!write_cmd(TCON_SYS_RUN)) { it8951_power_off(); return ESP_FAIL; }

    /* Validate-or-abort: geometry gate (also the anti-mis-flash gate). */
    if (!read_and_validate_dev_info()) {
        ESP_LOGE(TAG, "panel validation FAILED — not an E1003/ED103TC2, or "
                      "SPI comms are broken. Refusing to drive the panel.");
        it8951_power_off();
        return ESP_FAIL;
    }

    if (!reg_write(REG_I80CPCR, 0x0001)) { it8951_power_off(); return ESP_FAIL; }
    if (!force_temp())                   { it8951_power_off(); return ESP_FAIL; }
    if (!read_vcom())                    { it8951_power_off(); return ESP_FAIL; }

    s_state = PWR_VALIDATED;
    ESP_LOGI(TAG, "init OK");
    return ESP_OK;
}

bool it8951_panel_ok(void)   { return s_state == PWR_VALIDATED; }
uint16_t it8951_vcom_mv(void){ return s_vcom_mv; }

/* ── Frame load + refresh ───────────────────────────────────────────── */

static bool load_frame(const uint8_t *fb)
{
    if (!reg_write(REG_LISAR + 2, (uint16_t)(s_img_buf_addr >> 16))) return false;
    if (!reg_write(REG_LISAR,     (uint16_t)(s_img_buf_addr)))       return false;

    if (!write_cmd(TCON_LD_IMG_AREA)) return false;
    if (!write_data16((LD_LITTLE_ENDIAN << 8) | (LD_4BPP << 4) | LD_ROTATE_0)) return false;
    if (!write_data16(0)) return false;
    if (!write_data16(0)) return false;
    if (!write_data16(IT8951_PANEL_W)) return false;
    if (!write_data16(IT8951_PANEL_H)) return false;

    /* Stream all rows in ONE CS-low session with the bus held exclusively:
     * toggling CS mid-load resets the controller's write pointer, and the
     * shared-bus SD card must not win the bus between row transmits. */
    spi_device_acquire_bus(s_spi, portMAX_DELAY);
    if (!wait_hrdy(HRDY_TIMEOUT_MS)) { spi_device_release_bus(s_spi); return false; }
    cs_low();
    spi_tx16(PRE_WRITE);                        /* single data preamble */

    for (int y = 0; y < IT8951_PANEL_H; y++) {
        const uint8_t *src = fb + (size_t)y * ROW_BYTES;
        /* Mirror the row at 16-bit-word granularity — ED103TC2 scans rows
         * right-to-left (bytes within each word keep their order). */
        for (int wi = 0; wi < ROW_WORDS; wi++) {
            const uint8_t *sw = src + (ROW_WORDS - 1 - wi) * 2;
            s_rowbuf[wi * 2]     = sw[0];
            s_rowbuf[wi * 2 + 1] = sw[1];
        }
        spi_transaction_t t = { .length = ROW_BYTES * 8, .tx_buffer = s_rowbuf };
        esp_err_t err = spi_device_polling_transmit(s_spi, &t);
        if (err != ESP_OK) {
            cs_high();
            spi_device_release_bus(s_spi);
            ESP_LOGE(TAG, "row %d transmit failed: %s", y, esp_err_to_name(err));
            return false;
        }
    }

    cs_high();
    spi_device_release_bus(s_spi);
    return write_cmd(TCON_LD_IMG_END);
}

static bool display_area(uint16_t mode)
{
    return write_cmd(CMD_DPY_AREA) &&
           write_data16(0) && write_data16(0) &&
           write_data16(IT8951_PANEL_W) && write_data16(IT8951_PANEL_H) &&
           write_data16(mode);
}

esp_err_t it8951_display_full(const uint8_t *fb)
{
    esp_err_t err = it8951_init();
    if (err != ESP_OK) return err;

    /* Wake + re-force temperature (harmless if already awake; the reference
     * forces temp before every update). */
    if (!write_cmd(TCON_SYS_RUN)) return ESP_FAIL;
    if (!force_temp())            return ESP_FAIL;
    if (!wait_display_idle(DISPLAY_TIMEOUT_MS)) return ESP_FAIL;

    int64_t t0 = esp_timer_get_time();
    if (!load_frame(fb)) { ESP_LOGE(TAG, "frame load failed"); return ESP_FAIL; }
    int64_t t1 = esp_timer_get_time();
    ESP_LOGI(TAG, "frame load: %lld ms", (t1 - t0) / 1000);

    /* Anti-ghosting INIT (white) clear, then the actual GC16 image. The
     * image is already loaded, so the previous picture stays up during the
     * load and only the brief clear precedes the new one. */
    if (!display_area(MODE_INIT)) return ESP_FAIL;
    bool ok_init = wait_display_idle(DISPLAY_TIMEOUT_MS);
    int64_t t2 = esp_timer_get_time();

    if (!display_area(MODE_GC16)) return ESP_FAIL;
    bool ok_gc16 = wait_display_idle(DISPLAY_TIMEOUT_MS);
    int64_t t3 = esp_timer_get_time();

    /* Log measured refresh times — M1 uses these to size DISPLAY_TIMEOUT_MS
     * comfortably above the real worst case. */
    ESP_LOGI(TAG, "refresh: INIT %lld ms (%s), GC16 %lld ms (%s)",
             (t2 - t1) / 1000, ok_init ? "ok" : "TIMEOUT",
             (t3 - t2) / 1000, ok_gc16 ? "ok" : "TIMEOUT");

    return (ok_init && ok_gc16) ? ESP_OK : ESP_ERR_TIMEOUT;
}

/* ── Contract power-down ────────────────────────────────────────────── */

void it8951_power_off(void)
{
    /* 1. Never cut rails mid-waveform: wait for the display engine first.
     *    On timeout we still settle + SLEEP before dropping anything —
     *    the one thing we never do is yank the rails immediately. */
    if (s_spi && s_state >= PWR_ON) {
        if (s_state == PWR_VALIDATED && !wait_display_idle(DISPLAY_TIMEOUT_MS)) {
            ESP_LOGE(TAG, "power_off: display engine still busy after timeout — "
                          "settling 2 s before SLEEP + rail-drop");
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
        write_cmd(TCON_SLEEP);
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    /* 2. Release the SPI peripheral so SCLK/MOSI become plain GPIOs. */
    spi_teardown();

    /* 3. Drive every EPD signal line LOW before the rails drop — leaving
     *    them driven high would back-bias the controller through its ESD
     *    diodes as its supply collapses (the Spectra-6 firmware's hard-won
     *    lesson; the reference E1003 project omits this, we keep it). */
    const int sig_pins[] = { PIN_EPD_CS, PIN_EPD_RST, PIN_EPD_SCLK, PIN_EPD_MOSI };
    for (size_t i = 0; i < sizeof(sig_pins) / sizeof(sig_pins[0]); i++) {
        gpio_reset_pin(sig_pins[i]);
        gpio_set_direction(sig_pins[i], GPIO_MODE_OUTPUT);
        gpio_set_level(sig_pins[i], 0);
    }
    vTaskDelay(pdMS_TO_TICKS(100));

    /* Shared-bus SD: deselect low + rail off now that the bus is idle. */
    gpio_set_direction(PIN_SD_CS, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_SD_CS, 0);
    gpio_set_direction(PIN_SD_EN, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_SD_EN, 0);

    /* 4. Rails down in reverse order: bias first, then core. */
    gpio_set_direction(PIN_EPD_DRIVE_EN, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_EPD_DRIVE_EN, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_direction(PIN_ITE_VCC_EN, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_ITE_VCC_EN, 0);

    s_state = PWR_OFF;
    ESP_LOGI(TAG, "powered off (bias→core sequenced)");
}
