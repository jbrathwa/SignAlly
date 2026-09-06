/**
 * @file capture_stub.c
 * TEMPORARY placeholder implementation -- see capture_stub.h.
 *
 * Polled from the main loop (not an lv_timer) because it isn't bound to any
 * widget's lifetime and needs to check state every tick regardless of screen,
 * mirroring the esp_timer_get_time()-based now_ms() pattern buttons.c already
 * uses for its own debounce.
 */

#include "capture_stub.h"
#include "ui_center.h"
#include "buttons.h"
#include "esp_timer.h"
#include "esp_log.h"

static const char *TAG = "capture_stub";

/* Flip to true and reflash to force the Error path for manual testing. */
#define SIMULATE_ERROR false

/* TODO: replace with real landmark capture-complete signal from module 1 */
#define CAPTURE_STUB_LISTENING_MS 3000

/* TODO: replace with real API response once backend module is ready */
#define CAPTURE_STUB_ANALYZING_MS 1500

/* Mirrors data/demo_state.json's "result_text" -- keep the two in sync until
 * a real backend replaces this stub (see src/demo_manager.cpp for the JSON
 * path; not read from there directly because that module is C++-only and
 * this one is plain C). */
static const char *STUB_RESULT_TEXT =
    "The kitchen light is now on. Living room set to 22 degrees.";

static bool was_listening_active = false;
static int64_t listening_deadline_ms = 0;
static bool was_analyzing = false;
static int64_t analyzing_deadline_ms = 0;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

void capture_stub_poll(void)
{
    const ui_state_t state = ui_get_state();
    const int64_t now = now_ms();

    /* LISTENING -> ANALYZING after a fixed simulated capture delay */
    const bool listening_active = (state == UI_STATE_LISTENING) && buttons_capture_is_active();
    if (listening_active && !was_listening_active) {
        listening_deadline_ms = now + CAPTURE_STUB_LISTENING_MS;
    }
    was_listening_active = listening_active;

    if (listening_active && now >= listening_deadline_ms) {
        ESP_LOGI(TAG, "Simulated capture complete -> ANALYZING");
        ui_set_state(UI_STATE_ANALYZING);
    }

    /* ANALYZING -> RESULT/ERROR after a fixed simulated backend delay */
    const bool analyzing = (state == UI_STATE_ANALYZING);
    if (analyzing && !was_analyzing) {
        analyzing_deadline_ms = now + CAPTURE_STUB_ANALYZING_MS;
    }
    was_analyzing = analyzing;

    if (analyzing && now >= analyzing_deadline_ms) {
        if (SIMULATE_ERROR) {
            ESP_LOGI(TAG, "Simulated backend response -> ERROR (SIMULATE_ERROR)");
            ui_set_state(UI_STATE_ERROR);
        } else {
            ESP_LOGI(TAG, "Simulated backend response -> RESULT");
            ui_show_result(STUB_RESULT_TEXT);
            ui_set_state(UI_STATE_RESULT);
        }
    }
}
