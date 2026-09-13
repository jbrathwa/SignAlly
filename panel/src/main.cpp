/**
 * @file main.cpp
 * Main entry point for Elecrow CrowPanel 2.8" ESP32 HMI (LVGL v8)
 * Integrates pure C display, touch, and UI drivers with LittleFS JSON demo manager
 */

#include <stdio.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "Arduino.h"
#include "demo_manager.h"

/* Pure C Driver & UI Headers */
extern "C" {
    #include "lvgl.h"
    #include "display_driver.h"
    #include "touch_driver.h"
    #include "ui.h"
    #include "buttons.h"
    #include "capture_stub.h"
    #include "uart_link.h"
}

static const char *TAG = "main";

/**
 * High-resolution ESP timer callback to increment LVGL tick
 */
static void lv_tick_task(void *arg)
{
    (void)arg;
    lv_tick_inc(2); /* 2ms tick increment matching 2ms timer period */
}

/**
 * Initialize high resolution timer for LVGL tick management
 */
static void lv_tick_timer_init(void)
{
    const esp_timer_create_args_t periodic_timer_args = {
        .callback = &lv_tick_task,
        .name = "lv_tick_timer"
    };

    esp_timer_handle_t periodic_timer;
    ESP_ERROR_CHECK(esp_timer_create(&periodic_timer_args, &periodic_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(periodic_timer, 2000)); /* 2000 us = 2 ms */
    ESP_LOGI(TAG, "LVGL tick timer initialized (2ms interval)");
}

/**
 * Arduino/PlatformIO setup entry point
 */
void setup(void)
{
    Serial.begin(115200);
    ESP_LOGI(TAG, "Starting Elecrow CrowPanel 2.8\" ESP32 HMI (LVGL v8)...");

    /* 1. Initialize LVGL Library Core */
    lv_init();

    /* 2. Initialize Hardware Display Driver (ILI9341 240x320) */
    display_driver_init();

    /*
     * 3. Touch (XPT2046) -- DISABLED, not removed.
     *
     * Nothing in the UI is clickable any more: Start/Stop and mic mute are
     * physical buttons, so there is not a single event callback or hit area
     * left on any screen. Registering the indev anyway costs 10 SPI
     * transactions per LVGL cycle on the bus the display is sharing, to
     * produce coordinates nothing consumes.
     *
     * The driver itself is kept intact in src/touch_driver.c. Uncomment this
     * one line to bring touch back the moment a screen gains a touch target.
     * Note the calibration constants in include/touch_driver.h are still
     * wrong (labelled for landscape 320x240 while the panel runs portrait
     * 240x320) and will need measuring first.
     */
    /* touch_driver_init(); */

    /* 4. Initialize LVGL Tick Timer */
    lv_tick_timer_init();

    /* 5. Initialize HMI User Interface Layout */
    ui_init();

    /* 6. Initialize Physical Buttons (Start/Stop, Mic mute) -- after ui_init(),
     *    the handlers and the mute-flag seed both touch UI objects */
    buttons_init();

    /* 7. Open the UART link to the UNO Q MCU -- after ui_init(), because an
     *    inbound message can drive the UI on the very first poll */
    uart_link_init();

    /* 8. Mount LittleFS so the bench-only "reload" command has something to
     *    read. It no longer applies the file at boot, so nothing here touches
     *    the UI and the Boot/Intro sequence ui_init() started stands -- the
     *    explicit ui_set_state(UI_STATE_BOOT) that used to be needed to undo
     *    the JSON's "state" field is gone with it. The status bar now waits for
     *    a {"t":"status"} from the MCU. */
    demo_manager_init();

    ESP_LOGI(TAG, "System initialization complete. Running main loop.");
}

/**
 * Arduino/PlatformIO loop entry point
 */
void loop(void)
{
    /* Handle LVGL GUI Tasks */
    lv_timer_handler();

    /* Sample the physical buttons (debounced inside the input layer) */
    buttons_poll();

    /* Drain anything the MCU has sent. Non-blocking. */
    uart_link_poll();

    /* TEMPORARY: simulated Listening->Analyzing->Result/Error timing, see
     * src/capture_stub.c. Runs after buttons_poll() so a Start/Stop press
     * this tick is already reflected before the stub's edges are checked.
     *
     * Suppressed once the MCU has taken ownership of the state machine: both
     * drive the same transitions, and the stub's shorter ANALYZING timer would
     * win and paint its placeholder text over the real result. With no UART
     * peer this gate never closes and the panel runs the standalone demo
     * exactly as before -- see docs/STATE_MACHINE.md section 3. */
    if (!uart_link_owns_state()) {
        capture_stub_poll();
    }

    /* Check Serial Input for "reload" command */
    demo_manager_check_serial();

    /* Yield task execution */
    delay(5);
}
