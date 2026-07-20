/*
 * Hokku firmware for the Seeed reTerminal E1003 — 10.3" monochrome
 * 16-level-grayscale e-paper (ED103TC2 panel, IT8951 controller, 1404×1872).
 *
 * This is a port of the Hokku Spectra-6 frame firmware (../firmware/): the
 * application stack — state machine, WiFi, HTTP protocol, epoch-anchored
 * deep sleep, config schema, RTC log ring — is carried over near-verbatim;
 * the hardware layer (display driver, pins, power, battery, USB detect) is
 * replaced for this board. See it8951.c and pins.h for the hardware story
 * and README.md for the binding hardware-safety contract.
 *
 * State machine summary (unchanged from the Spectra-6 firmware):
 *   - USB_AWAKE     charger reports input power: full-power, logs on,
 *                   never deep-sleep
 *   - BATTERY_IDLE  no input power: 5 s awake window then deep sleep
 *   - DEEP_SLEEP    EXT1 wake on KEY0 (GPIO 3) or timer
 *   - REFRESH       transient — fetch + display, return to enclosing regime
 *
 * Board differences that shaped this port:
 *   - No USB-detect GPIO: the S3's native USB pins are I2C here. USB/charge
 *     presence is read from the SY6974B charger (I2C 0x6b, READ-ONLY).
 *     Consequently there is no USB-plug wake source — but none is needed:
 *     flashing goes through the CH340K, whose DTR/RTS auto-reset hard-resets
 *     the chip out of deep sleep.
 *   - Single-controller display, single 1,314,144-byte 4bpp buffer (no
 *     dual-half split), driven by it8951.c.
 *   - Battery: GPIO1/ADC1_CH0, ×2.0 divider, gated by VBAT_EN (GPIO40).
 *   - Refresh gating: a low battery REFUSES to start a refresh (the EPD
 *     bias pump is the board's biggest load; a brownout mid-waveform can
 *     permanently damage the panel).
 */

#include <stdarg.h>
#include <string.h>
#include <stdio.h>
#include <inttypes.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "esp_timer.h"
#include "esp_app_desc.h"

#include "pins.h"
#include "it8951.h"
#include "charger_sy6974b.h"

static const char *TAG = "hokku-e1003";

/* ── M1 bench-test mode ──────────────────────────────────────────────
 * Set to 1 to skip the whole application: boot → init the IT8951 →
 * display a 16-step gray ramp with an orientation marker → idle forever.
 * This is the M1 hardware gate: it proves SPI comms (GET_DEV_INFO),
 * 4bpp packing + row mirroring (clean monotonic bands, marker top-left),
 * the factory VCOM read-back, and logs the measured refresh time for
 * sizing timeouts. No WiFi, no server, no sleep. */
#define M1_RAMP_TEST 0

/* ── Display parameters ──────────────────────────────────────────────
 * Wire format = framebuffer format: full frame, 4bpp packed, 2 px/byte,
 * high nibble = left pixel, 0x0 black … 0xF white. The server sends
 * exactly TOTAL_IMAGE_SIZE bytes; there is no dual-half split. */
#define DISPLAY_W          IT8951_PANEL_W          /* 1872 — native landscape */
#define DISPLAY_H          IT8951_PANEL_H          /* 1404 */
#define TOTAL_IMAGE_SIZE   IT8951_FRAME_BYTES      /* 1,314,144 */
#define COLOR_WHITE_BYTE   0xFF

/* ── Network / timeouts ──────────────────────────────────────────────── */
#define WIFI_CONNECT_TIMEOUT_MS  8000   /* per-network attempt; two networks = 16 s worst case */
#define HTTP_TIMEOUT_MS          30000

/* ── Battery ─────────────────────────────────────────────────────────── */
#define BATT_LOW_MV        3400  /* below this (and not on power): refuse GC16 */

/* ── Regime timings (unchanged from the Spectra-6 firmware) ──────────── */
#define BATTERY_AWAKE_WINDOW_US  (5LL * 1000000LL)
#define POLL_INTERVAL_MS  100
#define BUTTON_DEBOUNCE_SAMPLES  2
#define SLEEP_FALLBACK_3H_US  (3LL * 3600 * 1000000LL)
#define REFRESH_RETRY_SECONDS           60
#define SERVER_BUSY_DISPLAY_THRESHOLD_S 20
#define BATT_LOW_RETRY_SECONDS          (6 * 3600)  /* battery-low: try again in 6 h */
#define MAX_SPURIOUS_RESETS  3

/* ── RTC memory (survives deep sleep + esp_restart) ──────────────────
 * Same design as the Spectra-6 firmware: RTC_NOINIT_ATTR everywhere, with
 * a magic check that zero-initialises exactly once after POR. */
#define RTC_MAGIC 0x484F4B45  /* "HOKE" — E1003 variant (differs from frame's "HOKU") */

RTC_NOINIT_ATTR static uint32_t rtc_magic;
RTC_NOINIT_ATTR static uint32_t boot_count;

RTC_NOINIT_ATTR static uint8_t  wifi_channel;
RTC_NOINIT_ATTR static uint8_t  wifi_bssid[6];
RTC_NOINIT_ATTR static bool     has_wifi_cache;
RTC_NOINIT_ATTR static uint8_t  last_wifi_index;

RTC_NOINIT_ATTR static uint16_t last_battery_mv;
RTC_NOINIT_ATTR static int32_t  last_sleep_seconds;
RTC_NOINIT_ATTR static int64_t  next_refresh_epoch;
RTC_NOINIT_ATTR static int64_t  pre_sleep_server_epoch;
RTC_NOINIT_ATTR static int32_t  last_sleep_err_s;
RTC_NOINIT_ATTR static bool     last_sleep_err_known;
RTC_NOINIT_ATTR static uint8_t  consecutive_spurious_resets;

#define LAST_SLEEP_MODE_NONE            0
#define LAST_SLEEP_MODE_TIMER_WAKE      1
#define LAST_SLEEP_MODE_BUTTON_WAKE     2
/* mode 3 (USB_PLUG) existed on the Spectra-6 board; the E1003 has no USB
 * wake source, so it can never occur here. Value kept reserved. */
#define LAST_SLEEP_MODE_BUTTON_USB      4
#define LAST_SLEEP_MODE_BUTTON_BATT     5
#define LAST_SLEEP_MODE_USB_SCHED       6
#define LAST_SLEEP_MODE_SPURIOUS        7
#define LAST_SLEEP_MODE_POST_REFRESH    8
RTC_NOINIT_ATTR static uint8_t  last_sleep_mode;

#define ACTION_NONE          0
#define ACTION_REFRESH       1
#define ACTION_ENTER_REGIME  2
RTC_NOINIT_ATTR static uint8_t  pending_action;

/* ── Log ring buffer (6 KB, RTC slow memory — survives deep sleep) ──── */
#define LOG_RING_SIZE 6144
RTC_NOINIT_ATTR static char     s_log_ring[LOG_RING_SIZE];
RTC_NOINIT_ATTR static uint16_t s_log_ring_head;
RTC_NOINIT_ATTR static uint16_t s_log_ring_used;
static portMUX_TYPE s_log_ring_mux = portMUX_INITIALIZER_UNLOCKED;

/* ── Forward declarations ────────────────────────────────────────────── */
static void log_level_apply(bool usb_awake);

/* ── Shared globals ──────────────────────────────────────────────────── */
static EventGroupHandle_t  wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static bool last_wifi_used_cache = false;
static const char *current_regime = "boot";

/* Charger state cache. I2C is only ever touched from the main task (boot
 * classification + regime polls); the LED-blink task reads these caches so
 * there is no cross-task I2C access. */
static volatile bool s_cached_usb_present = false;
static volatile bool s_cached_charging    = false;

#include "version.h"
#include "config.h"
#include "text_render.h"

/* ═══════════════════════════════════════════════════════════════════
 *  Display helpers (single mono buffer — no dual-half split)
 * ═══════════════════════════════════════════════════════════════════ */

/* Display a text message on the panel. White background, black text. */
static void display_message(const char *msg)
{
    uint8_t *fb = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) {
        ESP_LOGE(TAG, "Cannot allocate framebuffer for message");
        return;
    }
    memset(fb, COLOR_WHITE_BYTE, TOTAL_IMAGE_SIZE);
    draw_string(fb, DISPLAY_W, DISPLAY_H, 40, 60, msg, 0x0, 5);
    if (it8951_display_full(fb) != ESP_OK) {
        ESP_LOGE(TAG, "message display failed");
    }
    heap_caps_free(fb);
}

/* ═══════════════════════════════════════════════════════════════════
 *  WiFi (carried over verbatim from the Spectra-6 firmware)
 * ═══════════════════════════════════════════════════════════════════ */

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(wifi_events, WIFI_FAIL_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        /* Set the bit BEFORE logging: fwrite in log_ring_vprintf can block if
         * the console TX buffer is full, which would prevent the bit from ever
         * being set and make wifi_connect() time out even though we have an IP. */
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&e->ip_info.ip));
    }
}

static bool wifi_inited = false;

static void wifi_init_once(void)
{
    if (wifi_inited) return;
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t h1, h2;
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, &h1);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, &h2);
    wifi_inited = true;
}

#define WIFI_TRY(expr) do {                                             \
    esp_err_t __err = (expr);                                           \
    if (__err != ESP_OK) {                                              \
        ESP_LOGW(TAG, "%s -> %s (continuing)", #expr, esp_err_to_name(__err)); \
        return false;                                                   \
    }                                                                   \
} while (0)

static bool wifi_connect(void)
{
    if (wifi_events == NULL) {
        wifi_events = xEventGroupCreate();
    } else {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
    }
    wifi_init_once();

    WIFI_TRY(esp_wifi_set_mode(WIFI_MODE_STA));
    WIFI_TRY(esp_wifi_start());

    int first = (config.wifi_order == WIFI_ORDER_LAST_FIRST) ? (int)last_wifi_index : 0;
    for (int step = 0; step < 2; step++) {
        int idx = (first + step) % 2;
        if (config.wifi_ssid[idx][0] == '\0') continue;

        wifi_config_t wifi_cfg = {
            .sta = {
                .threshold.authmode = WIFI_AUTH_OPEN,
                .pmf_cfg = { .capable = true, .required = false },
            },
        };
        strncpy((char *)wifi_cfg.sta.ssid, config.wifi_ssid[idx], sizeof(wifi_cfg.sta.ssid) - 1);
        wifi_cfg.sta.ssid[sizeof(wifi_cfg.sta.ssid) - 1] = '\0';
        strncpy((char *)wifi_cfg.sta.password, config.wifi_pass[idx], sizeof(wifi_cfg.sta.password) - 1);
        wifi_cfg.sta.password[sizeof(wifi_cfg.sta.password) - 1] = '\0';

        if (has_wifi_cache && wifi_channel > 0 && idx == last_wifi_index) {
            wifi_cfg.sta.channel = wifi_channel;
            memcpy(wifi_cfg.sta.bssid, wifi_bssid, 6);
            wifi_cfg.sta.bssid_set = true;
            ESP_LOGI(TAG, "WiFi fast reconnect ch=%d (net %d)", wifi_channel, idx);
        }

        WIFI_TRY(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
        WIFI_TRY(esp_wifi_connect());

        EventBits_t bits = xEventGroupWaitBits(wifi_events,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

        if (bits & WIFI_CONNECTED_BIT) {
            wifi_ap_record_t ap;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
                wifi_channel = ap.primary;
                memcpy(wifi_bssid, ap.bssid, 6);
                has_wifi_cache = true;
            }
            last_wifi_used_cache = wifi_cfg.sta.bssid_set;
            last_wifi_index = (uint8_t)idx;
            return true;
        }

        if (wifi_cfg.sta.bssid_set) {
            ESP_LOGW(TAG, "Fast reconnect failed for net %d, retrying with full scan...", idx);
            has_wifi_cache = false;
            esp_wifi_disconnect();

            wifi_cfg.sta.channel = 0;
            wifi_cfg.sta.bssid_set = false;
            WIFI_TRY(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
            xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
            WIFI_TRY(esp_wifi_connect());

            bits = xEventGroupWaitBits(wifi_events,
                WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

            if (bits & WIFI_CONNECTED_BIT) {
                wifi_ap_record_t ap;
                if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
                    wifi_channel = ap.primary;
                    memcpy(wifi_bssid, ap.bssid, 6);
                    has_wifi_cache = true;
                }
                last_wifi_used_cache = false;
                last_wifi_index = (uint8_t)idx;
                return true;
            }
        }

        int next_idx = (first + step + 1) % 2;
        if (step < 1 && config.wifi_ssid[next_idx][0] != '\0') {
            ESP_LOGW(TAG, "WiFi net %d failed, trying net %d...", idx, next_idx);
            esp_wifi_disconnect();
        }
    }

    ESP_LOGE(TAG, "WiFi connect failed");
    return false;
}

static void wifi_shutdown(void)
{
    esp_wifi_disconnect();
    esp_wifi_stop();
}

/* ═══════════════════════════════════════════════════════════════════
 *  HTTP Image Download (protocol identical to the Spectra-6 firmware,
 *  plus the X-Panel-Type header that requests the mono wire format)
 * ═══════════════════════════════════════════════════════════════════ */

typedef struct {
    uint8_t *buf;
    size_t   received;
    size_t   capacity;
    char     sleep_seconds_hdr[32];
    char     server_epoch_hdr[32];
} http_download_ctx_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_download_ctx_t *ctx = (http_download_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;

    switch (evt->event_id) {
        case HTTP_EVENT_ON_CONNECTED:
            ctx->received = 0;
            ctx->sleep_seconds_hdr[0] = '\0';
            ctx->server_epoch_hdr[0]  = '\0';
            break;
        case HTTP_EVENT_ON_HEADER:
            if (evt->header_key && evt->header_value) {
                if (strcasecmp(evt->header_key, "X-Sleep-Seconds") == 0) {
                    strncpy(ctx->sleep_seconds_hdr, evt->header_value,
                            sizeof(ctx->sleep_seconds_hdr) - 1);
                    ctx->sleep_seconds_hdr[sizeof(ctx->sleep_seconds_hdr) - 1] = '\0';
                } else if (strcasecmp(evt->header_key, "X-Server-Time-Epoch") == 0) {
                    strncpy(ctx->server_epoch_hdr, evt->header_value,
                            sizeof(ctx->server_epoch_hdr) - 1);
                    ctx->server_epoch_hdr[sizeof(ctx->server_epoch_hdr) - 1] = '\0';
                }
            }
            break;
        case HTTP_EVENT_ON_DATA:
            if (ctx->received + evt->data_len <= ctx->capacity) {
                memcpy(ctx->buf + ctx->received, evt->data, evt->data_len);
                ctx->received += evt->data_len;
            }
            break;
        default:
            break;
    }
    return ESP_OK;
}

static void build_frame_state_json(char *buf, size_t buflen,
                                   const char *wake_label,
                                   int64_t boot_time_us)
{
    int rssi = 0;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        rssi = ap.rssi;
    }

    size_t free_heap = esp_get_free_heap_size();

    const esp_app_desc_t *app = esp_app_get_description();
    const char *fw = (app && app->version[0]) ? app->version : "unknown";

    /* "usb" semantics on this board: charger PG_STAT (any valid input power
     * — computer USB or wall adapter both count, unlike the Spectra-6
     * board's host-detect-only signal). */
    const char *usb = s_cached_usb_present ? "host" : "none";

    time_t clk_now_t = time(NULL);
    int64_t clk_now = (clk_now_t < 1577836800) ? 0 : (int64_t)clk_now_t;

    const char *last_sleep_str =
        (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE)   ? "timer_wake" :
        (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_WAKE)  ? "button_wake" :
        (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_USB)   ? "button_usb" :
        (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_BATT)  ? "button_batt" :
        (last_sleep_mode == LAST_SLEEP_MODE_USB_SCHED)    ? "usb_sched" :
        (last_sleep_mode == LAST_SLEEP_MODE_SPURIOUS)     ? "spurious" :
        (last_sleep_mode == LAST_SLEEP_MODE_POST_REFRESH) ? "post_refresh" :
        "none";

    int64_t uptime_s = (esp_timer_get_time() - boot_time_us) / 1000000LL;

    char sleep_err_buf[16];
    if (last_sleep_err_known) {
        snprintf(sleep_err_buf, sizeof(sleep_err_buf), "%d", (int)last_sleep_err_s);
    } else {
        strcpy(sleep_err_buf, "null");
    }

    snprintf(buf, buflen,
        "{\"fw\":\"%s\",\"boot\":%u,\"wake\":\"%s\",\"regime\":\"%s\","
        "\"uptime_s\":%lld,\"bat_mv\":%d,\"usb\":\"%s\","
        "\"last_sleep\":\"%s\",\"rssi\":%d,\"heap_kb\":%u,"
        "\"spurious\":%u,\"cfg_ver\":%u,\"clk_now\":%lld,"
        "\"next_ep\":%lld,\"sleep_err_s\":%s,\"wifi_cached\":%s}",
        fw, (unsigned)boot_count, wake_label, current_regime,
        (long long)uptime_s, (int)last_battery_mv, usb,
        last_sleep_str, rssi, (unsigned)(free_heap / 1024u),
        (unsigned)consecutive_spurious_resets,
        (unsigned)config.cfg_ver, (long long)clk_now,
        (long long)(next_refresh_epoch > 0 ? next_refresh_epoch : 0LL),
        sleep_err_buf,
        last_wifi_used_cache ? "true" : "false");
}

static uint8_t *download_image(int32_t *out_sleep_seconds, int64_t *out_server_epoch,
                               int *out_http_status,
                               const char *wake_label,
                               int64_t boot_time_us)
{
    uint8_t *buf = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGE(TAG, "Failed to allocate image buffer from PSRAM");
        return NULL;
    }

    http_download_ctx_t ctx = { .buf = buf, .received = 0, .capacity = TOTAL_IMAGE_SIZE };

    esp_http_client_config_t http_cfg = {
        .url = config.image_url,
        .event_handler = http_event_handler,
        .user_data = &ctx,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
    };

    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (!client) {
        ESP_LOGE(TAG, "esp_http_client_init failed (OOM?)");
        heap_caps_free(buf);
        return NULL;
    }

    esp_http_client_set_method(client, HTTP_METHOD_POST);

    if (config.screen_name[0] != '\0') {
        esp_http_client_set_header(client, "X-Screen-Name", config.screen_name);
    }

    /* Panel-type declaration: tells the server to serve the mono16 wire
     * format (1404×1872 @4bpp, single buffer) instead of the Spectra-6
     * format. Screens default to spectra6 server-side when absent. */
    esp_http_client_set_header(client, "X-Panel-Type", "mono16_e1003");

    char frame_state[384];
    build_frame_state_json(frame_state, sizeof(frame_state),
                           wake_label ? wake_label : "unknown",
                           boot_time_us);
    esp_http_client_set_header(client, "X-Frame-State", frame_state);

    /* Attach ring-buffer log as POST body (plain text). */
    char *log_body = NULL;
    int   log_body_len = 0;
    uint16_t snap_head, snap_used;
    taskENTER_CRITICAL(&s_log_ring_mux);
    snap_head = s_log_ring_head;
    snap_used = s_log_ring_used;
    taskEXIT_CRITICAL(&s_log_ring_mux);
    if (snap_used > 0) {
        log_body = malloc(snap_used);
        if (log_body) {
            uint16_t start = (snap_used < LOG_RING_SIZE) ? 0 : snap_head;
            for (uint16_t i = 0; i < snap_used; i++) {
                log_body[i] = s_log_ring[(start + i) % LOG_RING_SIZE];
            }
            log_body_len = (int)snap_used;
        }
    }
    if (log_body) {
        esp_http_client_set_header(client, "Content-Type", "text/plain");
        esp_http_client_set_post_field(client, log_body, log_body_len);
    }

    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);

    if (ctx.sleep_seconds_hdr[0] != '\0' && out_sleep_seconds != NULL) {
        int32_t secs = atoi(ctx.sleep_seconds_hdr);
        if (secs > 0) {
            *out_sleep_seconds = secs;
            ESP_LOGI(TAG, "X-Sleep-Seconds: %d", secs);
        } else {
            ESP_LOGW(TAG, "X-Sleep-Seconds present but non-positive: '%s' (parsed=%d)",
                     ctx.sleep_seconds_hdr, (int)secs);
        }
    } else {
        ESP_LOGW(TAG, "X-Sleep-Seconds header missing from response (status=%d)", status);
    }
    if (ctx.server_epoch_hdr[0] != '\0' && out_server_epoch != NULL) {
        int64_t epoch = atoll(ctx.server_epoch_hdr);
        if (epoch > 0) {
            *out_server_epoch = epoch;
            struct timeval tv = { .tv_sec = (time_t)epoch, .tv_usec = 0 };
            settimeofday(&tv, NULL);
            struct tm t;
            gmtime_r(&tv.tv_sec, &t);
            char timestamp[40];
            strftime(timestamp, sizeof(timestamp), "%Y-%m-%d %H:%M:%S UTC", &t);
            ESP_LOGI(TAG, "X-Server-Time-Epoch: %lld — system clock set to %s",
                     epoch, timestamp);
        } else {
            ESP_LOGW(TAG, "X-Server-Time-Epoch present but non-positive: '%s'", ctx.server_epoch_hdr);
        }
    } else {
        ESP_LOGW(TAG, "X-Server-Time-Epoch header missing from response (status=%d)", status);
    }

    esp_http_client_cleanup(client);
    free(log_body);

    if (err != ESP_OK || status != 200) {
        ESP_LOGE(TAG, "HTTP download failed: err=%s status=%d", esp_err_to_name(err), status);
        if (out_http_status) *out_http_status = status;
        heap_caps_free(buf);
        return NULL;
    }

    taskENTER_CRITICAL(&s_log_ring_mux);
    s_log_ring_head = 0;
    s_log_ring_used = 0;
    taskEXIT_CRITICAL(&s_log_ring_mux);

    if (ctx.received != TOTAL_IMAGE_SIZE) {
        ESP_LOGE(TAG, "Image size mismatch: got %d, expected %d — is the server "
                      "serving the mono16_e1003 wire format?",
                 (int)ctx.received, TOTAL_IMAGE_SIZE);
        heap_caps_free(buf);
        return NULL;
    }

    if (out_http_status) *out_http_status = status;
    ESP_LOGI(TAG, "Downloaded %d bytes", (int)ctx.received);
    return buf;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Battery (GPIO1/ADC1_CH0, ×2.0 divider, VBAT_EN-gated)
 * ═══════════════════════════════════════════════════════════════════ */

static int read_battery_mv(void)
{
    /* The divider is behind a FET on VBAT_EN — assert it only around the
     * measurement (left high it leaks ~200 µA through the 20 kΩ chain). */
    gpio_set_level(PIN_VBAT_EN, 1);
    vTaskDelay(pdMS_TO_TICKS(20));  /* divider + filter cap (10 nF) settle */

    adc_oneshot_unit_handle_t handle;
    adc_oneshot_unit_init_cfg_t init = { .unit_id = ADC_UNIT_1 };
    if (adc_oneshot_new_unit(&init, &handle) != ESP_OK) {
        gpio_set_level(PIN_VBAT_EN, 0);
        return 0;
    }

    /* ×2.0 divider → max ~2.1 V at the pin at 4.2 V pack → 12 dB atten. */
    adc_oneshot_chan_cfg_t chan = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT };
    adc_oneshot_config_channel(handle, ADC_CHANNEL_0, &chan);

    adc_cali_handle_t cali = NULL;
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    bool calibrated = (adc_cali_create_scheme_curve_fitting(&cali_cfg, &cali) == ESP_OK);

    int raw_sum = 0;
    int good_reads = 0;
    for (int i = 0; i < 50; i++) {
        int raw = 0;
        if (adc_oneshot_read(handle, ADC_CHANNEL_0, &raw) == ESP_OK) {
            raw_sum += raw;
            good_reads++;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    gpio_set_level(PIN_VBAT_EN, 0);

    if (good_reads == 0) {
        ESP_LOGE("BATT", "all 50 ADC reads failed");
        if (cali) adc_cali_delete_scheme_curve_fitting(cali);
        adc_oneshot_del_unit(handle);
        return 0;
    }
    int raw_avg = raw_sum / good_reads;

    int mv = 0;
    if (calibrated) {
        adc_cali_raw_to_voltage(cali, raw_avg, &mv);
        adc_cali_delete_scheme_curve_fitting(cali);
    } else {
        mv = (raw_avg * 3100) / 4095;  /* DB_12 range ~3100 mV */
    }

    int battery_mv = (int)(mv * VBAT_DIVIDER_MULT);

    ESP_LOGI("BATT", "ADC raw_avg=%d, calibrated_mv=%d, battery=%d mV",
             raw_avg, mv, battery_mv);

    adc_oneshot_del_unit(handle);
    return battery_mv;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Charge-indicator task — blinks USER_LED at 1 Hz while charging.
 *  Reads only the volatile caches (main task owns all I2C traffic).
 * ═══════════════════════════════════════════════════════════════════ */

static TaskHandle_t chg_monitor_task_handle = NULL;

static void chg_monitor_task(void *arg)
{
    bool led_on = false;
    while (1) {
        if (s_cached_charging) {
            led_on = !led_on;
            gpio_set_level(PIN_USER_LED, led_on ? USER_LED_ON : USER_LED_OFF);
        } else {
            gpio_set_level(PIN_USER_LED, USER_LED_OFF);
            led_on = false;
        }
        vTaskDelay(pdMS_TO_TICKS(500));  /* 1 Hz blink */
    }
}

static void chg_monitor_start(void)
{
    if (!chg_monitor_task_handle) {
        xTaskCreate(chg_monitor_task, "chg_mon", 2048, NULL, 1, &chg_monitor_task_handle);
    }
}

static void chg_monitor_stop(void)
{
    if (chg_monitor_task_handle) {
        vTaskDelete(chg_monitor_task_handle);
        chg_monitor_task_handle = NULL;
    }
}

/* ═══════════════════════════════════════════════════════════════════
 *  Power-state detection (SY6974B over I2C — replaces the Spectra-6
 *  board's USB_DETECT GPIO; PG covers wall adapters too, an improvement)
 * ═══════════════════════════════════════════════════════════════════ */

static bool usb_host_present(void)
{
    bool usb = false, chg = false;
    if (charger_read_status(&usb, &chg)) {
        s_cached_usb_present = usb;
        s_cached_charging    = chg;
        return usb;
    }
    return false;  /* charger unreachable → assume battery */
}

/* Debounced input-power detection: flipped only after N consecutive
 * opposite reads (regime loops poll at POLL_INTERVAL_MS). */
#define USB_DEBOUNCE_SAMPLES  3
static int s_usb_stable_level = 0;    /* 0 = no power (start pessimistic) */
static int s_usb_opposite_streak = 0;
static bool usb_host_present_stable(void)
{
    int cur = usb_host_present() ? 1 : 0;
    if (cur != s_usb_stable_level) {
        if (++s_usb_opposite_streak >= USB_DEBOUNCE_SAMPLES) {
            s_usb_stable_level = cur;
            s_usb_opposite_streak = 0;
        }
    } else {
        s_usb_opposite_streak = 0;
    }
    return s_usb_stable_level == 1;
}

/* Debounced KEY0 read (KEY0 = the top "refresh" button per the Zephyr
 * board port; bench-verify which physical button this is on your unit). */
static int s_btn_low_count = 0;
static bool s_btn_reported = false;
static bool button_pressed_debounced(void)
{
    int lvl = gpio_get_level(PIN_KEY0);
    if (lvl == 0) {
        if (s_btn_low_count < 255) s_btn_low_count++;
        if (s_btn_low_count >= BUTTON_DEBOUNCE_SAMPLES && !s_btn_reported) {
            s_btn_reported = true;
            return true;
        }
    } else {
        s_btn_low_count = 0;
        s_btn_reported = false;
    }
    return false;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Schedule helpers (server-epoch anchored — verbatim)
 * ═══════════════════════════════════════════════════════════════════ */

static time_t now_epoch(void)
{
    time_t t = time(NULL);
    return (t < 1577836800) ? 0 : t;
}

static bool refresh_due(void)
{
    if (next_refresh_epoch == 0) return true;
    if (next_refresh_epoch < 0)
        return esp_timer_get_time() >= -next_refresh_epoch;
    time_t now = now_epoch();
    if (now == 0) return false;
    return now >= next_refresh_epoch;
}

static void schedule_retry_in(int seconds, const char *reason)
{
    time_t now = now_epoch();
    if (now > 0) {
        next_refresh_epoch = (int64_t)now + seconds;
    } else {
        next_refresh_epoch = -(esp_timer_get_time() + (int64_t)seconds * 1000000LL);
    }
    pre_sleep_server_epoch = 0;
    last_sleep_err_known = false;
    ESP_LOGW(TAG, "Refresh retry in %d s (%s)", seconds, reason);
}

/* ═══════════════════════════════════════════════════════════════════
 *  Logging regime control + RTC ring hook (verbatim)
 * ═══════════════════════════════════════════════════════════════════ */

static void log_level_apply(bool usb_awake)
{
    esp_log_level_set("*", usb_awake ? ESP_LOG_INFO : ESP_LOG_NONE);
}

#define LOG_BUF_COUNT 2
#define LOG_BUF_SIZE  512

static char         *s_log_bufs[LOG_BUF_COUNT];
static volatile bool s_log_buf_used[LOG_BUF_COUNT];

static int log_ring_vprintf(const char *fmt, va_list args)
{
    int slot = -1;
    taskENTER_CRITICAL(&s_log_ring_mux);
    for (int i = 0; i < LOG_BUF_COUNT; i++) {
        if (!s_log_buf_used[i]) { s_log_buf_used[i] = true; slot = i; break; }
    }
    taskEXIT_CRITICAL(&s_log_ring_mux);

    if (slot < 0) return vprintf(fmt, args);

    int len = vsnprintf(s_log_bufs[slot], LOG_BUF_SIZE, fmt, args);
    if (len < 0) len = 0;
    if (len >= LOG_BUF_SIZE) len = LOG_BUF_SIZE - 1;

    if (len > 0) fwrite(s_log_bufs[slot], 1, (size_t)len, stdout);

    taskENTER_CRITICAL(&s_log_ring_mux);
    for (int i = 0; i < len; i++) {
        s_log_ring[s_log_ring_head] = s_log_bufs[slot][i];
        s_log_ring_head = (s_log_ring_head + 1) % LOG_RING_SIZE;
        if (s_log_ring_used < LOG_RING_SIZE) s_log_ring_used++;
    }
    s_log_buf_used[slot] = false;
    taskEXIT_CRITICAL(&s_log_ring_mux);

    return len;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Deep sleep entry — E1003 contract teardown
 * ═══════════════════════════════════════════════════════════════════ */

static void save_pre_sleep_epoch(int64_t server_epoch, int64_t local_time_at_download_us)
{
    if (server_epoch <= 0) {
        pre_sleep_server_epoch = 0;
        last_sleep_err_known = false;
        return;
    }
    int64_t delta_s = (esp_timer_get_time() - local_time_at_download_us) / 1000000LL;
    pre_sleep_server_epoch = server_epoch + delta_s;
}

/* EXT1 wake on KEY0 only. No USB wake source exists on this board — see
 * the file-header note (CH340K auto-reset covers reflash reachability,
 * and input-power changes are picked up at the next timer wake). */
static void enter_deep_sleep(int64_t sleep_us)
{
    ESP_LOGI(TAG, "Deep sleep for %lld s (next-refresh-epoch=%lld)",
             sleep_us / 1000000LL, (long long)next_refresh_epoch);

    chg_monitor_stop();

    /* Contract power-down: display idle → SLEEP → signals low → bias off →
     * core off. Idempotent — safe even if the panel was never touched. */
    it8951_power_off();

    gpio_set_level(PIN_USER_LED, USER_LED_OFF);
    gpio_set_level(PIN_VBAT_EN, 0);
    gpio_set_level(PIN_SD_EN, 0);
    gpio_set_level(PIN_SD_CS, 0);   /* SD is unpowered — a high CS would
                                     * backpower the card through its pad */
    gpio_set_level(PIN_TOUCH_RES, 0);  /* GT911 stays in reset (low power) */

    /* Timer wake */
    if (sleep_us > 0) {
        esp_sleep_enable_timer_wakeup((uint64_t)sleep_us);
    }

    /* EXT1 wake: KEY0 (active LOW, external 10K pull-up). */
    esp_sleep_enable_ext1_wakeup((1ULL << PIN_KEY0), ESP_EXT1_WAKEUP_ANY_LOW);
    rtc_gpio_init(PIN_KEY0);
    rtc_gpio_pullup_en(PIN_KEY0);

    /* Latch every power-relevant output through deep sleep. GPIO39/40 are
     * non-RTC (digital-domain) pins, so a plain drive would float once the
     * chip sleeps; holds keep them at their driven level. GPIO11 is held
     * too — the reference project misses it and its bias-switch enable
     * floats in sleep (partial re-enable risk). */
    const int hold_pins[] = {
        PIN_EPD_DRIVE_EN, PIN_ITE_VCC_EN,               /* rails: LOW */
        PIN_EPD_CS, PIN_EPD_RST, PIN_EPD_SCLK, PIN_EPD_MOSI, /* signals: LOW */
        PIN_SD_EN, PIN_SD_CS, PIN_VBAT_EN,              /* LOW */
        PIN_TOUCH_RES,                                  /* GT911 in reset, LOW */
        PIN_USER_LED,                                   /* off */
    };
    for (size_t i = 0; i < sizeof(hold_pins)/sizeof(hold_pins[0]); i++) {
        gpio_hold_en(hold_pins[i]);
    }
    gpio_deep_sleep_hold_en();

    /* Isolate the input pins we own (RTC-capable only, 0–21). */
    rtc_gpio_isolate(PIN_VBAT_ADC);
    rtc_gpio_isolate(PIN_EPD_HRDY);
    rtc_gpio_isolate(PIN_EPD_MISO);

    rtc_magic = RTC_MAGIC;
    esp_deep_sleep_start();
}

/* ═══════════════════════════════════════════════════════════════════
 *  Refresh action (fetch + display)
 * ═══════════════════════════════════════════════════════════════════ */

static bool perform_refresh(const char *wake_label, int64_t boot_time_us)
{
    esp_log_level_set("*", ESP_LOG_INFO);

    /* HW-safety gate: never start a GC16 refresh on a low battery without
     * input power. The EPD bias pump is the board's biggest load; a sag-
     * induced brownout mid-waveform leaves DC on the glass (panel damage).
     * No display_message here either — that would run the pump too. */
    if (last_battery_mv > 500 && last_battery_mv < BATT_LOW_MV &&
        !s_cached_usb_present) {
        ESP_LOGE(TAG, "battery %u mV < %d mV and no input power — refusing "
                      "refresh, retrying in %d h",
                 last_battery_mv, BATT_LOW_MV, BATT_LOW_RETRY_SECONDS / 3600);
        schedule_retry_in(BATT_LOW_RETRY_SECONDS, "battery low");
        log_level_apply(usb_host_present());
        return false;
    }

    int32_t sleep_seconds = 0;
    int64_t server_epoch = 0;
    int      http_status = 0;
    int64_t local_time_at_download_us = 0;
    uint8_t *img = NULL;

    if (!wifi_connect()) {
        ESP_LOGE(TAG, "WiFi connect failed");
        char wifi_err_msg[256];
        snprintf(wifi_err_msg, sizeof(wifi_err_msg),
                 "WiFi connect failed.\n"
                 "\n"
                 "Will retry in %d s.\n"
                 "Press button to\n"
                 "try again now.",
                 REFRESH_RETRY_SECONDS);
        display_message(wifi_err_msg);
        schedule_retry_in(REFRESH_RETRY_SECONDS, "wifi_connect failed");
        log_level_apply(usb_host_present());
        return false;
    }

    img = download_image(&sleep_seconds, &server_epoch, &http_status, wake_label, boot_time_us);
    local_time_at_download_us = esp_timer_get_time();

    wifi_shutdown();

    if (server_epoch > 0 && sleep_seconds > 0) {
        next_refresh_epoch = server_epoch + sleep_seconds;
        last_sleep_seconds = sleep_seconds;
        save_pre_sleep_epoch(server_epoch, local_time_at_download_us);
        ESP_LOGI(TAG, "Next refresh scheduled for epoch %lld (in %d s)",
                 (long long)next_refresh_epoch, (int)sleep_seconds);
    } else if (img) {
        schedule_retry_in(REFRESH_RETRY_SECONDS,
                          "server response missing/invalid X-Sleep-Seconds");
    }

    if (!img) {
        if (http_status == 503 && sleep_seconds > 0) {
            if (sleep_seconds > SERVER_BUSY_DISPLAY_THRESHOLD_S) {
                char msg[128];
                snprintf(msg, sizeof(msg),
                         "Server not ready.\n"
                         "\n"
                         "Retrying in %d s.\n"
                         "Press reset to try\n"
                         "again now.",
                         (int)sleep_seconds);
                display_message(msg);
            }
            schedule_retry_in((int)sleep_seconds, "server busy (503)");
        } else {
            char msg[384];
            snprintf(msg, sizeof(msg),
                     "Image download failed.\n"
                     "\n"
                     "Tried to connect to:\n"
                     "%s\n"
                     "\n"
                     "Will retry in %d s.\n"
                     "Press reset to try\n"
                     "again now.",
                     config.image_url, REFRESH_RETRY_SECONDS);
            display_message(msg);
            schedule_retry_in(REFRESH_RETRY_SECONDS, "download failed");
        }
        log_level_apply(usb_host_present());
        return false;
    }

    ESP_LOGI(TAG, "Displaying image...");
    esp_err_t derr = it8951_display_full(img);
    heap_caps_free(img);
    if (derr != ESP_OK) {
        ESP_LOGE(TAG, "Display failed (%s) — image not shown. Will retry on "
                      "the next scheduled cycle.", esp_err_to_name(derr));
        log_level_apply(usb_host_present());
        return false;
    }
    ESP_LOGI(TAG, "Image displayed.");

    log_level_apply(usb_host_present());
    return true;
}

/* ═══════════════════════════════════════════════════════════════════
 *  Regimes (unchanged structure)
 * ═══════════════════════════════════════════════════════════════════ */

static void regime_usb_awake(int64_t boot_time_us);
static void regime_battery_idle(int64_t boot_time_us);

static void trigger_restart(uint8_t action, uint8_t sleep_mode)
{
    pending_action  = action;
    last_sleep_mode = sleep_mode;
    chg_monitor_stop();
    /* Contract: the panel must be sequenced down before ANY exit — an
     * esp_restart resets the GPIO matrix, which would otherwise float the
     * rail enables with the panel in an arbitrary state. */
    it8951_power_off();
    gpio_set_level(PIN_USER_LED, USER_LED_OFF);
    vTaskDelay(pdMS_TO_TICKS(50));
    esp_restart();
}

static void regime_usb_awake(int64_t boot_time_us)
{
    current_regime = "usb_awake";
    log_level_apply(true);
    ESP_LOGI(TAG, "Entering USB_AWAKE regime");

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));

        if (!usb_host_present_stable()) {
            ESP_LOGI(TAG, "Input power gone — transitioning to BATTERY_IDLE");
            regime_battery_idle(boot_time_us);
            return;
        }

        if (button_pressed_debounced()) {
            trigger_restart(ACTION_REFRESH, LAST_SLEEP_MODE_BUTTON_USB);
            return;
        }

        if (refresh_due()) {
            trigger_restart(ACTION_REFRESH, LAST_SLEEP_MODE_USB_SCHED);
            return;
        }
    }
}

static void regime_battery_idle(int64_t boot_time_us)
{
    current_regime = "battery_idle";
    log_level_apply(true);
    ESP_LOGI(TAG, "Entering BATTERY_IDLE regime (%d s awake window)",
             (int)(BATTERY_AWAKE_WINDOW_US / 1000000LL));

    int64_t deadline_us = esp_timer_get_time() + BATTERY_AWAKE_WINDOW_US;

    while (esp_timer_get_time() < deadline_us) {
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));

        if (usb_host_present_stable()) {
            ESP_LOGI(TAG, "Power plugged during battery window — switching to USB_AWAKE");
            regime_usb_awake(boot_time_us);
            return;
        }

        if (button_pressed_debounced()) {
            trigger_restart(ACTION_REFRESH, LAST_SLEEP_MODE_BUTTON_BATT);
            return;
        }
    }

    time_t now = now_epoch();
    int64_t sleep_us;
    if (next_refresh_epoch > 0 && now > 0 && next_refresh_epoch > now) {
        sleep_us = (int64_t)(next_refresh_epoch - now) * 1000000LL;
    } else {
        sleep_us = SLEEP_FALLBACK_3H_US;
    }
    enter_deep_sleep(sleep_us);
    /* Never returns */
}

/* ═══════════════════════════════════════════════════════════════════
 *  Hardware init
 * ═══════════════════════════════════════════════════════════════════ */

static void hw_gpio_init(void)
{
    /* Release any holds/isolation from a previous deep sleep FIRST. */
    gpio_deep_sleep_hold_dis();
    const int held_pins[] = {
        PIN_EPD_DRIVE_EN, PIN_ITE_VCC_EN,
        PIN_EPD_CS, PIN_EPD_RST, PIN_EPD_SCLK, PIN_EPD_MOSI,
        PIN_SD_EN, PIN_SD_CS, PIN_VBAT_EN, PIN_TOUCH_RES, PIN_USER_LED,
    };
    for (size_t i = 0; i < sizeof(held_pins)/sizeof(held_pins[0]); i++) {
        gpio_hold_dis(held_pins[i]);
    }
    const int rtc_pins[] = {
        PIN_VBAT_ADC, PIN_EPD_HRDY, PIN_EPD_MISO, PIN_KEY0,
    };
    for (size_t i = 0; i < sizeof(rtc_pins)/sizeof(rtc_pins[0]); i++) {
        if (rtc_gpio_is_valid_gpio(rtc_pins[i])) {
            rtc_gpio_hold_dis(rtc_pins[i]);
            rtc_gpio_deinit(rtc_pins[i]);
        }
        gpio_reset_pin(rtc_pins[i]);
    }

    /* Board housekeeping outputs. The EPD pins themselves are owned by
     * it8951.c and configured there (only when a display is needed —
     * boots that never display never power the panel). */
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << PIN_SD_CS) | (1ULL << PIN_SD_EN) |
                        (1ULL << PIN_VBAT_EN) | (1ULL << PIN_USER_LED) |
                        (1ULL << PIN_ITE_VCC_EN) | (1ULL << PIN_EPD_DRIVE_EN) |
                        (1ULL << PIN_TOUCH_RES),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&out_cfg);
    gpio_set_level(PIN_ITE_VCC_EN, 0);     /* panel stays dark until needed */
    gpio_set_level(PIN_EPD_DRIVE_EN, 0);
    gpio_set_level(PIN_SD_CS, 1);          /* park the shared-bus SD deselected */
    gpio_set_level(PIN_SD_EN, 0);          /* SD rail off — card unused */
    gpio_set_level(PIN_VBAT_EN, 0);
    gpio_set_level(PIN_USER_LED, USER_LED_OFF);
    gpio_set_level(PIN_TOUCH_RES, 0);      /* GT911 held in reset — we never
                                            * use touch; unheld it burns ~5 mA */

    /* Buttons: inputs with pull-ups (board has external 10Ks too). */
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << PIN_KEY0) | (1ULL << PIN_KEY1) | (1ULL << PIN_KEY2),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&btn_cfg);
}

/* ═══════════════════════════════════════════════════════════════════
 *  M1 bench test: 16-step gray ramp with orientation marker
 * ═══════════════════════════════════════════════════════════════════ */

#if M1_RAMP_TEST
static void m1_ramp_test(void)
{
    ESP_LOGI(TAG, "=== M1 RAMP TEST ===");
    uint8_t *fb = heap_caps_malloc(TOTAL_IMAGE_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) { ESP_LOGE(TAG, "fb alloc failed"); return; }

    /* 16 vertical bands, black (0) on the LEFT → white (15) on the RIGHT. */
    for (int y = 0; y < DISPLAY_H; y++) {
        uint8_t *row = fb + (size_t)y * (DISPLAY_W / 2);
        for (int x = 0; x < DISPLAY_W; x += 2) {
            uint8_t g0 = (uint8_t)((x)     * 16 / DISPLAY_W);
            uint8_t g1 = (uint8_t)((x + 1) * 16 / DISPLAY_W);
            row[x / 2] = (uint8_t)((g0 << 4) | g1);  /* high nibble = left px */
        }
    }
    /* Orientation marker: solid black 120×120 square in the TOP-LEFT corner
     * (drawn over the black end of the ramp — outlined by a white border so
     * it is visible). If it appears top-right, row mirroring is wrong; if
     * bottom-left, the vertical order is wrong. */
    for (int y = 0; y < 140; y++) {
        uint8_t *row = fb + (size_t)y * (DISPLAY_W / 2);
        for (int x = 0; x < 140; x += 2) {
            bool border = (y >= 120) || (x >= 120);
            row[x / 2] = border ? 0xFF : 0x00;
        }
    }
    draw_string(fb, DISPLAY_W, DISPLAY_H, 40, DISPLAY_H - 120,
                "HOKKU E1003 M1  BLACK<--RAMP-->WHITE", 0x0, 4);

    int64_t t0 = esp_timer_get_time();
    esp_err_t err = it8951_display_full(fb);
    ESP_LOGI(TAG, "M1 result: %s, total %lld ms, VCOM=%u mV",
             esp_err_to_name(err),
             (esp_timer_get_time() - t0) / 1000, it8951_vcom_mv());
    heap_caps_free(fb);

    ESP_LOGI(TAG, "M1 checklist: 16 clean monotonic bands? marker TOP-LEFT? "
                  "VCOM ~1400? note the refresh ms for timeout sizing.");
    /* Idle forever — no sleep, console stays up for inspection. */
    while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}
#endif

/* ═══════════════════════════════════════════════════════════════════
 *  app_main
 * ═══════════════════════════════════════════════════════════════════ */

// cppcheck-suppress unusedFunction
void app_main(void)
{
    int64_t boot_time = esp_timer_get_time();

    /* ── Step 1: validate RTC state ───────────────────────────────── */
    if (rtc_magic != RTC_MAGIC) {
        rtc_magic = 0;
        boot_count = 0;
        wifi_channel = 0;
        memset(wifi_bssid, 0, sizeof(wifi_bssid));
        has_wifi_cache = false;
        last_wifi_index = 0;
        last_battery_mv = 0;
        last_sleep_seconds = 0;
        next_refresh_epoch = 0;
        pre_sleep_server_epoch = 0;
        last_sleep_err_s = 0;
        last_sleep_err_known = false;
        consecutive_spurious_resets = 0;
        last_sleep_mode = LAST_SLEEP_MODE_NONE;
        pending_action = ACTION_NONE;
        s_log_ring_head = 0;
        s_log_ring_used = 0;
        struct timeval tv = {0, 0};
        settimeofday(&tv, NULL);
    }
    rtc_magic = RTC_MAGIC;

    /* Dual-output log hook: serial + RTC ring buffer. */
    bool log_bufs_ok = true;
    for (int i = 0; i < LOG_BUF_COUNT; i++) {
        s_log_bufs[i] = heap_caps_malloc(LOG_BUF_SIZE, MALLOC_CAP_SPIRAM);
        if (!s_log_bufs[i]) { log_bufs_ok = false; break; }
    }
    if (log_bufs_ok) esp_log_set_vprintf(log_ring_vprintf);

    ESP_LOGI(TAG, "Firmware %s (E1003)  built %s", FW_VERSION_STRING, FW_BUILD_TIMESTAMP);

    boot_count++;

    /* ── Step 2: handle deep-sleep wakes ────────────────────────────
     * Timer / KEY0 wakes: restart with ACTION_REFRESH for clean driver
     * state (same design as the Spectra-6 firmware). There is no USB
     * wake source on this board, so no USB_PLUG branch. */
    {
        esp_sleep_wakeup_cause_t sleep_cause = esp_sleep_get_wakeup_cause();
        if (sleep_cause == ESP_SLEEP_WAKEUP_TIMER ||
            sleep_cause == ESP_SLEEP_WAKEUP_EXT1) {
            uint64_t pins = (sleep_cause == ESP_SLEEP_WAKEUP_EXT1)
                            ? esp_sleep_get_ext1_wakeup_status() : 0;
            if (sleep_cause == ESP_SLEEP_WAKEUP_TIMER ||
                (pins & (1ULL << PIN_KEY0))) {
                pending_action  = ACTION_REFRESH;
                last_sleep_mode = (sleep_cause == ESP_SLEEP_WAKEUP_TIMER)
                                  ? LAST_SLEEP_MODE_TIMER_WAKE
                                  : LAST_SLEEP_MODE_BUTTON_WAKE;
                esp_restart();
            } else {
                if (consecutive_spurious_resets < MAX_SPURIOUS_RESETS) {
                    consecutive_spurious_resets++;
                    pending_action  = ACTION_ENTER_REGIME;
                    last_sleep_mode = LAST_SLEEP_MODE_SPURIOUS;
                    esp_restart();
                }
                consecutive_spurious_resets = 0;
                pending_action = ACTION_ENTER_REGIME;
            }
        }
    }

    /* ── Step 3: capture and clear pending_action ──────────────────── */
    uint8_t action = pending_action;
    pending_action = ACTION_NONE;

    /* ── Step 4: NVS + config ──────────────────────────────────────── */
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    config_load();

    /* ── Step 5: hardware init ─────────────────────────────────────── */
    hw_gpio_init();
    charger_init();
    usb_host_present();          /* prime the cached charger state */
    /* Prime the debouncer too, else the first regime poll sees a stale
     * "no power" initial state and hops USB_AWAKE→BATTERY_IDLE→back once
     * per boot (cosmetic 300 ms flap, inherited from the original FW). */
    s_usb_stable_level = s_cached_usb_present ? 1 : 0;
    chg_monitor_start();

    log_level_apply(s_cached_usb_present);

    ESP_LOGI(TAG, "Boot #%" PRIu32 ", action=%u, last_sleep=%u, power=%s%s",
             boot_count, (unsigned)action, (unsigned)last_sleep_mode,
             s_cached_usb_present ? "external" : "battery",
             s_cached_charging ? " (charging)" : "");

    last_battery_mv = read_battery_mv();
    ESP_LOGI(TAG, "Battery: %d mV", last_battery_mv);

#if M1_RAMP_TEST
    log_level_apply(true);
    m1_ramp_test();              /* never returns */
#endif

    /* ── Step 6: dispatch on action ────────────────────────────────── */
    consecutive_spurious_resets = 0;

    if (action == ACTION_NONE || action == ACTION_REFRESH) {
        if (!config_version_ok()) {
            char msg[256];
            snprintf(msg, sizeof(msg),
                     "Config version\nmismatch.\n\n"
                     "Expected: %d\nFound: %d\n\n"
                     "Run hokku-setup to\nreconfigure.",
                     CONFIG_VERSION, config.cfg_ver);
            display_message(msg);
            regime_battery_idle(boot_time);
            return;
        }
        if (!config_is_valid()) {
            display_message(
                "Hokku installed but\n"
                "cannot read config.\n\n"
                "Connect USB and run\n"
                "hokku-setup to\n"
                "configure."
            );
            regime_battery_idle(boot_time);
            return;
        }

        const char *label =
            (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE)  ? "timer" :
            (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_WAKE) ? "button_wake" :
            (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_USB)  ? "button_usb" :
            (last_sleep_mode == LAST_SLEEP_MODE_BUTTON_BATT) ? "button_batt" :
            (last_sleep_mode == LAST_SLEEP_MODE_USB_SCHED)   ? "usb_sched" :
            "first_boot";

        current_regime = s_cached_usb_present ? "usb_awake" : "battery_idle";

        int64_t prior_sleep_entry_epoch = pre_sleep_server_epoch;
        int32_t prior_sleep_duration    = last_sleep_seconds;

        perform_refresh(label, boot_time);

        if (last_sleep_mode == LAST_SLEEP_MODE_TIMER_WAKE &&
            prior_sleep_entry_epoch > 0 && prior_sleep_duration > 0) {
            time_t now = now_epoch();
            if (now > 0) {
                int64_t actual_slept_s = (int64_t)now - prior_sleep_entry_epoch;
                int64_t err            = actual_slept_s - prior_sleep_duration;
                if (err > INT32_MAX) err = INT32_MAX;
                if (err < INT32_MIN) err = INT32_MIN;
                last_sleep_err_s     = (int32_t)err;
                last_sleep_err_known = true;
            }
        }

        trigger_restart(ACTION_ENTER_REGIME, LAST_SLEEP_MODE_POST_REFRESH);
    }

    /* ── Step 7: enter the regime that matches current power state ── */
    if (usb_host_present()) {
        regime_usb_awake(boot_time);
    } else {
        regime_battery_idle(boot_time);
    }
    /* Never returns */
}
