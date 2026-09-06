/**
 * @file ui.c
 * HMI User Interface Main Setup for Portrait 240x320 Elecrow CrowPanel 2.8"
 */

#include "ui.h"
#include "esp_log.h"

static const char *TAG = "ui";

void ui_init(void)
{
    ESP_LOGI(TAG, "Creating Portrait HMI UI layout (240x320)...");

    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, UI_COLOR_BG, LV_PART_MAIN);

    /*
     * Screens are scrollable by default. Nothing here scrolls, and with no
     * input device registered nothing could scroll them anyway -- but this
     * stays as one line of cheap insurance for whenever touch comes back,
     * since a scroll started by jitter makes LVGL drop the CLICKED event for
     * that press and silently swallow taps near a widget edge.
     */
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* 1. Create Top Right-Aligned Status Bar (240x36) */
    ui_status_bar_create(scr);

    /* 2. Create Center Dynamic 6-State Section (240x284) */
    ui_center_create(scr);

    ESP_LOGI(TAG, "Portrait HMI UI created successfully");
}
