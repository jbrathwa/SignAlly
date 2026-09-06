/**
 * @file ui.h
 * HMI User Interface Aggregator for Portrait 240x320 Elecrow CrowPanel 2.8"
 */

#ifndef UI_H
#define UI_H

#include <stdint.h>
#include "lvgl.h"
#include "ui_theme.h"
#include "ui_status_bar.h"
#include "ui_center.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Create and initialize the complete Portrait HMI UI layout.
 */
void ui_init(void);

/* Removed: ui_update_tick(uint32_t). It was an empty stub that nothing ever
 * called -- the UI is entirely event-driven off ui_set_state(). Re-add a real
 * periodic hook here if the backend ever needs one. */

#ifdef __cplusplus
}
#endif

#endif /* UI_H */
