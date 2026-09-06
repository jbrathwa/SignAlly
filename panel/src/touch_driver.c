/**
 * @file touch_driver.c
 * Pure C Touch Driver implementation for XPT2046 on Elecrow CrowPanel 2.8" ESP32 HMI
 */

#include "touch_driver.h"
#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"

static const char *TAG = "touch_driver";

static spi_device_handle_t spi_touch = NULL;
static lv_indev_drv_t indev_drv;
static uint16_t last_valid_x = 0;
static uint16_t last_valid_y = 0;

/* TEMPORARY CALIBRATION DIAGNOSTIC -- remove once constants are corrected */
static uint16_t last_raw_x = 0;
static uint16_t last_raw_y = 0;

#define CMD_READ_X  0xD0
#define CMD_READ_Y  0x90
#define CMD_READ_Z1 0xB0
#define CMD_READ_Z2 0xC0

/**
 * Send command and receive 12-bit ADC result from XPT2046
 */
static uint16_t xpt2046_read_cmd(uint8_t cmd)
{
    uint8_t tx_buf[3] = {cmd, 0x00, 0x00};
    uint8_t rx_buf[3] = {0, 0, 0};

    spi_transaction_t t;
    memset(&t, 0, sizeof(t));
    t.length = 24;
    t.tx_buffer = tx_buf;
    t.rx_buffer = rx_buf;

    if (spi_touch != NULL) {
        spi_device_polling_transmit(spi_touch, &t);
    }

    /* Extract 12-bit ADC result from 24-bit SPI stream */
    uint16_t raw_val = ((uint16_t)rx_buf[1] << 8 | rx_buf[2]) >> 3;
    return raw_val;
}

/**
 * Compare function for qsort median filtering
 */
static int cmp_uint16(const void *a, const void *b)
{
    return (*(uint16_t*)a - *(uint16_t*)b);
}

/**
 * Read raw filtered X and Y values from XPT2046
 */
static bool xpt2046_get_raw_samples(uint16_t *out_x, uint16_t *out_y)
{
    uint16_t samples_x[5];
    uint16_t samples_y[5];

    for (int i = 0; i < 5; i++) {
        samples_x[i] = xpt2046_read_cmd(CMD_READ_X);
        samples_y[i] = xpt2046_read_cmd(CMD_READ_Y);
    }

    qsort(samples_x, 5, sizeof(uint16_t), cmp_uint16);
    qsort(samples_y, 5, sizeof(uint16_t), cmp_uint16);

    /* Use median sample (index 2) */
    uint16_t raw_x = samples_x[2];
    uint16_t raw_y = samples_y[2];

    /* Check pressure / bounds validity */
    if (raw_x < 150 || raw_x > 3950 || raw_y < 150 || raw_y > 3950) {
        return false;
    }

    *out_x = raw_x;
    *out_y = raw_y;
    return true;
}

bool touch_driver_read(uint16_t *x, uint16_t *y)
{
    uint16_t raw_x = 0, raw_y = 0;

    if (!xpt2046_get_raw_samples(&raw_x, &raw_y)) {
        return false;
    }

    /* Convert raw ADC to 240x320 portrait screen coordinates */
    int32_t calc_x = (int32_t)(raw_x - TOUCH_X_MIN) * DISP_HOR_RES / (TOUCH_X_MAX - TOUCH_X_MIN);
    int32_t calc_y = (int32_t)(raw_y - TOUCH_Y_MIN) * DISP_VER_RES / (TOUCH_Y_MAX - TOUCH_Y_MIN);

    /* Clamp coordinates within 240x320 screen bounds */
    if (calc_x < 0) calc_x = 0;
    if (calc_x >= DISP_HOR_RES) calc_x = DISP_HOR_RES - 1;

    if (calc_y < 0) calc_y = 0;
    if (calc_y >= DISP_VER_RES) calc_y = DISP_VER_RES - 1;

    last_raw_x = raw_x;
    last_raw_y = raw_y;

    *x = (uint16_t)calc_x;
    *y = (uint16_t)calc_y;

    last_valid_x = *x;
    last_valid_y = *y;

    return true;
}

/**
 * LVGL Input Device Callback
 */
static void touch_read_cb(lv_indev_drv_t *drv, lv_indev_data_t *data)
{
    uint16_t x = 0, y = 0;

    static bool was_pressed = false;

    if (touch_driver_read(&x, &y)) {
        data->state = LV_INDEV_STATE_PR;
        data->point.x = x;
        data->point.y = y;

        /* TEMPORARY: log once per press edge so the serial output stays readable */
        if (!was_pressed) {
            printf("TOUCH raw=(%4u,%4u)  mapped=(%3u,%3u)\n",
                   last_raw_x, last_raw_y, x, y);
            was_pressed = true;
        }
    } else {
        was_pressed = false;
        data->state = LV_INDEV_STATE_REL;
        data->point.x = last_valid_x;
        data->point.y = last_valid_y;
    }
}

void touch_driver_init(void)
{
    ESP_LOGI(TAG, "Initializing XPT2046 Touch Controller (GPIO 33)...");

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 2500000,      /* 2.5 MHz for XPT2046 */
        .mode = 0,                      /* SPI Mode 0 */
        .spics_io_num = TOUCH_CS_PIN,   /* Touch CS GPIO 33 */
        .queue_size = 3,
        .flags = SPI_DEVICE_NO_DUMMY
    };

    esp_err_t ret = spi_bus_add_device(display_get_spi_host(), &devcfg, &spi_touch);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add XPT2046 SPI device: %s", esp_err_to_name(ret));
        return;
    }

    /* Register LVGL Input Device */
    lv_indev_drv_init(&indev_drv);
    indev_drv.type = LV_INDEV_TYPE_POINTER;
    indev_drv.read_cb = touch_read_cb;

    /*
     * Resistive panels jitter by several pixels during a press. LVGL suppresses
     * LV_EVENT_CLICKED entirely once a press drifts past scroll_limit and a
     * scroll begins (lv_indev.c: "Send CLICK if no scrolling"), so the stock
     * 10px limit silently eats taps. Nothing in this UI scrolls, so raise it
     * well beyond the noise floor.
     */
    indev_drv.scroll_limit = 40;

    lv_indev_drv_register(&indev_drv);

    ESP_LOGI(TAG, "XPT2046 Touch Driver registered with LVGL");
    printf("TOUCH-DIAG: touch driver ready, tap to log coordinates\n");
}
