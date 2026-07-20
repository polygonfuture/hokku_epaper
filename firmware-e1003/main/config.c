#include "config.h"

#include <string.h>

#include "nvs_flash.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif

config_t config = {0};

/* E1003 addition: compiled-in fallback config from main/secrets.h
 * (gitignored). Applied only when NVS holds no valid config, so USB
 * provisioning via hokku-setup always takes precedence when present. */
static void apply_baked_defaults(void)
{
#ifdef HOKKU_WIFI_SSID
    if (config_is_valid()) return;          /* NVS wins */
    if (HOKKU_WIFI_SSID[0] == '\0') return; /* secrets.h not filled in */

    config.cfg_ver    = CONFIG_VERSION;
    config.wifi_order = WIFI_ORDER_PRIMARY_FIRST;
    strncpy(config.wifi_ssid[0], HOKKU_WIFI_SSID,   sizeof(config.wifi_ssid[0]) - 1);
    strncpy(config.wifi_pass[0], HOKKU_WIFI_PASS,   sizeof(config.wifi_pass[0]) - 1);
    strncpy(config.image_url,    HOKKU_IMAGE_URL,   sizeof(config.image_url) - 1);
    strncpy(config.screen_name,  HOKKU_SCREEN_NAME, sizeof(config.screen_name) - 1);
#endif
}

bool config_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("hokku", NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_u8(nvs, "cfg_ver",    &config.cfg_ver);
        nvs_get_u8(nvs, "wifi_order", &config.wifi_order);
        size_t len;
        len = sizeof(config.wifi_ssid[0]); nvs_get_str(nvs, "wifi_ssid1",  config.wifi_ssid[0], &len);
        len = sizeof(config.wifi_pass[0]); nvs_get_str(nvs, "wifi_pass1",  config.wifi_pass[0], &len);
        len = sizeof(config.wifi_ssid[1]); nvs_get_str(nvs, "wifi_ssid2",  config.wifi_ssid[1], &len);
        len = sizeof(config.wifi_pass[1]); nvs_get_str(nvs, "wifi_pass2",  config.wifi_pass[1], &len);
        len = sizeof(config.image_url);    nvs_get_str(nvs, "image_url",   config.image_url,    &len);
        len = sizeof(config.screen_name);  nvs_get_str(nvs, "screen_name", config.screen_name,  &len);
        nvs_close(nvs);
    }
    apply_baked_defaults();
    return true;
}

bool config_version_ok(void)
{
    return config.cfg_ver == CONFIG_VERSION;
}

bool config_is_valid(void)
{
    return config_version_ok()
        && config.wifi_ssid[0][0] != '\0'
        && config.image_url[0] != '\0';
}
