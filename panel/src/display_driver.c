/**
 * @file display_driver.c
 * Pure C Display Driver implementation for ILI9341V on Elecrow CrowPanel 2.8" ESP32 HMI
 */

#include "display_driver.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "display_driver";

/* Global/Static SPI Handles */
static spi_device_handle_t spi_tft = NULL;
static bool spi_bus_initialized = false;

/* LVGL Display Buffer & Driver Structs */
static lv_disp_draw_buf_t draw_buf;
static lv_color_t buf1[DISP_HOR_RES * 40];
static lv_color_t buf2[DISP_HOR_RES * 40];
static lv_disp_drv_t disp_drv;

/* ILI9341 Command Struct */
typedef struct {
    uint8_t cmd;
    uint8_t data[16];
    uint8_t databytes; /* Bit 7 set indicates delay after command */
} lcd_init_cmd_t;

/* ILI9341 Initialization Command Sequence */
static const lcd_init_cmd_t ili9341_init_cmds[] = {
    {0x01, {0}, 0x80},                                          /* Software Reset */
    {0xEF, {0x03, 0x80, 0x02}, 3},
    {0xCF, {0x00, 0xC1, 0x30}, 3},
    {0xED, {0x64, 0x03, 0x12, 0x81}, 4},
    {0xE8, {0x85, 0x00, 0x78}, 3},
    {0xCB, {0x39, 0x2C, 0x00, 0x34, 0x02}, 5},
    {0xF7, {0x20}, 1},
    {0xEA, {0x00, 0x00}, 2},
    {0xC0, {0x23}, 1},                                          /* Power control 1 */
    {0xC1, {0x10}, 1},                                          /* Power control 2 */
    {0xC5, {0x3E, 0x28}, 2},                                    /* VCOM control 1 */
    {0xC6, {0x86}, 1},                                          /* VCOM control 2 */
    {0x36, {0x48}, 1},                                          /* Memory Access Control (Portrait 240x320) */
    {0x3A, {0x55}, 1},                                          /* Pixel Format Set (16-bit RGB565) */
    {0xB1, {0x00, 0x1B}, 2},                                    /* Frame Rate Control (70 Hz) */
    {0xB6, {0x08, 0x82, 0x27}, 3},                              /* Display Function Control */
    {0xF2, {0x00}, 1},                                          /* 3Gamma Function Disable */
    {0x26, {0x01}, 1},                                          /* Gamma curve selected */
    {0xE0, {0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00}, 15}, /* Set Gamma */
    {0xE1, {0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F}, 15}, /* Set Gamma */
    {0x11, {0}, 0x80},                                          /* Sleep Out */
    {0x29, {0}, 0x80},                                          /* Display ON */
    {0, {0}, 0xFF}                                              /* End of commands marker */
};

/**
 * Send command to ILI9341
 */
static void lcd_cmd(spi_device_handle_t spi, const uint8_t cmd)
{
    gpio_set_level(DISP_TFT_DC, 0); /* Command mode: DC LOW */
    spi_transaction_t t;
    memset(&t, 0, sizeof(t));
    t.length = 8;
    t.tx_buffer = &cmd;
    spi_device_polling_transmit(spi, &t);
}

/**
 * Send data bytes to ILI9341
 */
static void lcd_data(spi_device_handle_t spi, const uint8_t *data, int len)
{
    if (len <= 0) return;
    gpio_set_level(DISP_TFT_DC, 1); /* Data mode: DC HIGH */
    spi_transaction_t t;
    memset(&t, 0, sizeof(t));
    t.length = len * 8;
    t.tx_buffer = data;
    spi_device_polling_transmit(spi, &t);
}

/**
 * Set LCD window address for pixel writing
 */
static void lcd_set_window(uint16_t x1, uint16_t y1, uint16_t x2, uint16_t y2)
{
    uint8_t data[4];
    
    /* Column Address Set */
    lcd_cmd(spi_tft, 0x2A);
    data[0] = (x1 >> 8) & 0xFF;
    data[1] = x1 & 0xFF;
    data[2] = (x2 >> 8) & 0xFF;
    data[3] = x2 & 0xFF;
    lcd_data(spi_tft, data, 4);

    /* Page Address Set */
    lcd_cmd(spi_tft, 0x2B);
    data[0] = (y1 >> 8) & 0xFF;
    data[1] = y1 & 0xFF;
    data[2] = (y2 >> 8) & 0xFF;
    data[3] = y2 & 0xFF;
    lcd_data(spi_tft, data, 4);

    /* Memory Write */
    lcd_cmd(spi_tft, 0x2C);
}

/**
 * LVGL Display Flush Callback
 */
static void display_flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *color_map)
{
    uint32_t size = (area->x2 - area->x1 + 1) * (area->y2 - area->y1 + 1);

    lcd_set_window(area->x1, area->y1, area->x2, area->y2);

    gpio_set_level(DISP_TFT_DC, 1); /* Data mode */

    spi_transaction_t t;
    memset(&t, 0, sizeof(t));
    t.length = size * 16; /* 16 bits per pixel (RGB565) */
    t.tx_buffer = color_map;
    spi_device_polling_transmit(spi_tft, &t);

    lv_disp_flush_ready(drv);
}

/**
 * Initialize GPIO pins for TFT DC & Backlight
 */
static void display_gpio_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << DISP_TFT_DC) | (1ULL << DISP_TFT_BCKL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&io_conf);

    /* Turn on backlight */
    gpio_set_level(DISP_TFT_BCKL, 1);
}

/**
 * Initialize Shared SPI Bus
 */
static void spi_bus_init(void)
{
    if (spi_bus_initialized) return;

    spi_bus_config_t buscfg = {
        .sclk_io_num = DISP_SPI_CLK,
        .mosi_io_num = DISP_SPI_MOSI,
        .miso_io_num = DISP_SPI_MISO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = DISP_HOR_RES * 40 * sizeof(lv_color_t) + 8
    };

    esp_err_t ret = spi_bus_initialize(DISP_SPI_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize SPI bus: %s", esp_err_to_name(ret));
        return;
    }

    spi_bus_initialized = true;
    ESP_LOGI(TAG, "SPI bus initialized successfully on HSPI/SPI2_HOST");
}

/**
 * Attach ILI9341 device to SPI Bus
 */
static void ili9341_spi_init(void)
{
    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 40 * 1000 * 1000, /* 40 MHz SPI Clock */
        .mode = 0,                           /* SPI Mode 0 */
        .spics_io_num = DISP_TFT_CS,         /* TFT CS GPIO 15 */
        .queue_size = 7,
        .flags = SPI_DEVICE_NO_DUMMY
    };

    esp_err_t ret = spi_bus_add_device(DISP_SPI_HOST, &devcfg, &spi_tft);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add ILI9341 SPI device: %s", esp_err_to_name(ret));
        return;
    }

    ESP_LOGI(TAG, "ILI9341 attached to SPI bus");
}

/**
 * Send initialization sequence to ILI9341
 */
static void ili9341_hardware_init(void)
{
    int cmd_idx = 0;
    while (ili9341_init_cmds[cmd_idx].databytes != 0xFF) {
        lcd_cmd(spi_tft, ili9341_init_cmds[cmd_idx].cmd);
        
        uint8_t len = ili9341_init_cmds[cmd_idx].databytes & 0x7F;
        if (len > 0) {
            lcd_data(spi_tft, ili9341_init_cmds[cmd_idx].data, len);
        }

        if (ili9341_init_cmds[cmd_idx].databytes & 0x80) {
            vTaskDelay(pdMS_TO_TICKS(120));
        }

        cmd_idx++;
    }
    ESP_LOGI(TAG, "ILI9341 hardware commands sent successfully");
}

spi_host_device_t display_get_spi_host(void)
{
    return DISP_SPI_HOST;
}

void display_set_backlight(bool enable)
{
    gpio_set_level(DISP_TFT_BCKL, enable ? 1 : 0);
}

void display_driver_init(void)
{
    ESP_LOGI(TAG, "Initializing ILI9341 Display Driver...");

    /* 1. Init GPIOs */
    display_gpio_init();

    /* 2. Init Shared SPI Bus */
    spi_bus_init();

    /* 3. Add ILI9341 to SPI Bus */
    ili9341_spi_init();

    /* 4. Send Hardware Init Commands */
    ili9341_hardware_init();

    /* 5. Initialize LVGL Display Driver */
    lv_disp_draw_buf_init(&draw_buf, buf1, buf2, DISP_HOR_RES * 40);

    lv_disp_drv_init(&disp_drv);
    disp_drv.hor_res = DISP_HOR_RES;
    disp_drv.ver_res = DISP_VER_RES;
    disp_drv.flush_cb = display_flush_cb;
    disp_drv.draw_buf = &draw_buf;

    lv_disp_drv_register(&disp_drv);

    ESP_LOGI(TAG, "Display driver registered with LVGL (320x240)");
}
