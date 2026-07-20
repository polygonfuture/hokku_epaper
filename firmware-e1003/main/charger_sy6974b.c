#include "charger_sy6974b.h"

#include "driver/i2c_master.h"
#include "esp_log.h"

#include "pins.h"

static const char *TAG = "sy6974b";

#define SY6974B_ADDR      0x6B
#define REG08_STATUS      0x08
/* REG08 bits (BQ2560x-compatible layout):
 *   [7:5] VBUS_STAT   [4:3] CHRG_STAT (00 idle, 01 pre, 10 fast, 11 done)
 *   [2]   PG_STAT     [1]   THERM_STAT   [0] VSYS_STAT                    */
#define PG_STAT_BIT       (1 << 2)
#define CHRG_STAT_MASK    0x18
#define CHRG_STAT_PRE     0x08
#define CHRG_STAT_FAST    0x10

static i2c_master_bus_handle_t s_bus = NULL;
static i2c_master_dev_handle_t s_dev = NULL;
static bool s_available = false;

static bool read_reg(uint8_t reg, uint8_t *out)
{
    if (!s_dev) return false;
    return i2c_master_transmit_receive(s_dev, &reg, 1, out, 1, 100) == ESP_OK;
}

bool charger_init(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = 0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,  /* board has externals too */
    };
    if (i2c_new_master_bus(&bus_cfg, &s_bus) != ESP_OK) {
        ESP_LOGW(TAG, "I2C bus init failed — treating as no charger");
        return false;
    }
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = SY6974B_ADDR,
        .scl_speed_hz = 100000,
    };
    if (i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev) != ESP_OK) {
        ESP_LOGW(TAG, "charger device add failed");
        return false;
    }

    uint8_t st = 0;
    s_available = read_reg(REG08_STATUS, &st);
    if (s_available) {
        ESP_LOGI(TAG, "probe OK, REG08=0x%02x (PG=%d chrg=%d)",
                 st, !!(st & PG_STAT_BIT), (st & CHRG_STAT_MASK) >> 3);
    } else {
        ESP_LOGW(TAG, "no ACK from 0x6b — USB/charge detection unavailable");
    }
    return s_available;
}

bool charger_available(void) { return s_available; }

bool charger_read_status(bool *usb_present, bool *charging)
{
    if (!s_available) return false;
    uint8_t st = 0;
    if (!read_reg(REG08_STATUS, &st)) return false;
    if (usb_present) *usb_present = (st & PG_STAT_BIT) != 0;
    if (charging) {
        uint8_t c = st & CHRG_STAT_MASK;
        *charging = (c == CHRG_STAT_PRE) || (c == CHRG_STAT_FAST);
    }
    return true;
}
