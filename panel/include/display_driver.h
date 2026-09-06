/**
 * @file display_driver.h
 * Pure C Display Driver interface for ILI9341V (320x240 SPI) on Elecrow CrowPanel 2.8" ESP32 HMI
 */

#ifndef DISPLAY_DRIVER_H
#define DISPLAY_DRIVER_H

#include <stdint.h>
#include <stdbool.h>
#include "lvgl.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Hardware Pin Definitions for Elecrow CrowPanel 2.8" */
#define DISP_SPI_MOSI      13
#define DISP_SPI_MISO      12
#define DISP_SPI_CLK       14
#define DISP_TFT_CS        15
#define DISP_TFT_DC        2
#define DISP_TFT_RST       -1  /* Tied to EN/RST */
#define DISP_TFT_BCKL      27  /* Active HIGH */

/* Display Dimensions (Portrait 240x320) */
#define DISP_HOR_RES       240
#define DISP_VER_RES       320

/* SPI Host Selection */
#define DISP_SPI_HOST      SPI2_HOST

/**
 * @brief Initialize SPI bus, ILI9341 display hardware, backlight, and LVGL display driver.
 */
void display_driver_init(void);

/**
 * @brief Set backlight state or brightness.
 * @param enable True to turn on backlight, false to turn off.
 */
void display_set_backlight(bool enable);

/**
 * @brief Get handle to initialized shared SPI host.
 * @return spi_host_device_t SPI Host ID.
 */
spi_host_device_t display_get_spi_host(void);

#ifdef __cplusplus
}
#endif

#endif /* DISPLAY_DRIVER_H */
