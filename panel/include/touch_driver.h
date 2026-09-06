/**
 * @file touch_driver.h
 * Pure C Touch Driver interface for XPT2046 SPI Touch Controller on Elecrow CrowPanel 2.8" ESP32 HMI
 */

#ifndef TOUCH_DRIVER_H
#define TOUCH_DRIVER_H

#include <stdint.h>
#include <stdbool.h>
#include "lvgl.h"
#include "display_driver.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Hardware Pin & Calibration Constants */
#define TOUCH_CS_PIN     33

/* Calibration Constants for 320x240 Display */
#define TOUCH_X_MIN      200
#define TOUCH_X_MAX      3700
#define TOUCH_Y_MIN      240
#define TOUCH_Y_MAX      3800

/**
 * @brief Initialize XPT2046 touch controller on SPI bus and register with LVGL.
 */
void touch_driver_init(void);

/**
 * @brief Read raw mapped screen coordinates from XPT2046 touch controller.
 * @param x Output pointer for X coordinate (0..320).
 * @param y Output pointer for Y coordinate (0..240).
 * @return true if screen is being touched/pressed, false otherwise.
 */
bool touch_driver_read(uint16_t *x, uint16_t *y);

#ifdef __cplusplus
}
#endif

#endif /* TOUCH_DRIVER_H */
