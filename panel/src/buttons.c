/**
 * @file buttons.c
 * Physical Button Input Layer implementation (Start/Stop + Mic mute)
 */

#include "buttons.h"
#include "ui_center.h"
#include "ui_status_bar.h"
#include "uart_link.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"

static const char *TAG = "buttons";

/* Mute flags -- this module is the single source of truth for both */
static bool mic_muted = false;
static bool isMuted = false;

/* Whether the device is actively trying to capture/watch for a sign. NOT
 * overall device power -- see buttons.h. Flipped by on_start_stop_pressed()
 * below, and kept in step with the orchestrator's screens through
 * buttons_set_capture_active(). */
static bool isCaptureActive = false;

/**
 * Per-button debounce state.
 *
 * Debouncing lives here, at the GPIO read level, rather than in the LVGL state
 * machine: ui_set_state() stays a pure view switch and knows nothing about
 * contact bounce. The edge is acted on the moment it is seen and the pin is
 * then locked out for BTN_DEBOUNCE_MS, so there is no press-to-response delay
 * and no minimum dwell on any screen -- the button is live again as soon as
 * the window closes.
 */
typedef struct {
    gpio_num_t pin;
    bool       was_pressed;      /* debounced level from the previous accept */
    int64_t    lockout_until_ms; /* ignore this pin until then */
} btn_t;

static btn_t btn_start_stop = { (gpio_num_t)BTN_START_STOP_GPIO, false, 0 };
static btn_t btn_speaker_mute = { (gpio_num_t)BTN_SPEAKER_MUTE_GPIO, false, 0 };

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/**
 * Return true once per accepted press (release edges only clear the latch).
 */
static bool button_pressed_edge(btn_t *btn)
{
    const int64_t now = now_ms();

    if (now < btn->lockout_until_ms) {
        return false;
    }

    /* Active-low: pressed pulls the pin to GND against the internal pull-up */
    const bool pressed = (gpio_get_level(btn->pin) == 0);

    if (pressed == btn->was_pressed) {
        return false;
    }

    btn->was_pressed = pressed;
    btn->lockout_until_ms = now + BTN_DEBOUNCE_MS;

    return pressed;
}

/**
 * Shared "Start/Stop pressed" handler.
 *
 * A plain toggle on isCaptureActive, NOT on the current screen. While capture
 * is on, the orchestrator moves the panel between LISTENING, ANALYZING, RESULT
 * and ERROR many times a minute -- "Nobody in frame" and "Move back" alone
 * land on ERROR every few seconds -- so a press can arrive on any of them and
 * must mean Stop on every one. Branching by screen used to send "start" from
 * RESULT and ERROR, which the orchestrator ignores while already capturing,
 * so Stop appeared to do nothing.
 *
 * Stop goes to INTRO immediately rather than waiting for the orchestrator's
 * `state idle`, which follows anyway. Start goes to LISTENING the same way.
 * The capture stub (src/capture_stub.c) is purely reactive to
 * ui_get_state()/buttons_capture_is_active() polling, so leaving LISTENING
 * here is enough to cancel it -- no separate cancel API needed.
 */
static void on_start_stop_pressed(void)
{
    if (ui_get_state() == UI_STATE_BOOT) {
        /* Ignored: no controls are live during the splash. */
        return;
    }

    isCaptureActive = !isCaptureActive;

    /* Leaving RESULT wipes the label so a stale sentence cannot flash on the
     * next RESULT before its text arrives -- same rule as uart_link.cpp. */
    ui_clear_result();
    ui_set_state(isCaptureActive ? UI_STATE_LISTENING : UI_STATE_INTRO);

    ESP_LOGI(TAG, "Start/Stop pressed -> isCaptureActive=%s, state=%d",
             isCaptureActive ? "true" : "false", (int)ui_get_state());

    /* Tell the MCU what the operator did here. Notification only -- the screen
     * has already moved, and the MCU does not ack it. Sent after the switch so
     * isCaptureActive is final; a no-op when no UART peer is attached. */
    uart_link_report_button(isCaptureActive ? "start" : "stop", false);
}

/*
 * Top-right push button. Toggles the SPEAKER, not the mic -- there is no
 * physical mic button on the enclosure.
 */
static void on_speaker_mute_pressed(void)
{
    buttons_set_speaker_muted(!isMuted);
    ESP_LOGI(TAG, "Speaker mute pressed -> %s", isMuted ? "MUTED" : "UNMUTED");
    /* isMuted is already the post-press value -- buttons_set_speaker_muted()
     * ran on the line above. Reporting the flag rather than a bare toggle is
     * what keeps the orchestrator's DeviceState.muted in step. */
    uart_link_report_button("mute", isMuted);
}

bool buttons_mic_is_muted(void)
{
    return mic_muted;
}

bool buttons_speaker_is_muted(void)
{
    return isMuted;
}

void buttons_set_mic_muted(bool muted)
{
    mic_muted = muted;
    ui_mic_set_state(muted ? MIC_MUTED : MIC_UNMUTED);
}

void buttons_set_speaker_muted(bool muted)
{
    isMuted = muted;
    ui_speaker_set_state(muted ? SPEAKER_MUTED : SPEAKER_UNMUTED);
}

bool buttons_capture_is_active(void)
{
    return isCaptureActive;
}

void buttons_set_capture_active(bool active)
{
    isCaptureActive = active;
}

void buttons_init(void)
{
    ESP_LOGI(TAG, "Initializing physical buttons (Start/Stop GPIO %d, Speaker mute GPIO %d)...",
             BTN_START_STOP_GPIO, BTN_SPEAKER_MUTE_GPIO);

    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << BTN_START_STOP_GPIO) | (1ULL << BTN_SPEAKER_MUTE_GPIO),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE
    };

    esp_err_t ret = gpio_config(&io_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure button GPIOs: %s", esp_err_to_name(ret));
        return;
    }

    /* Seed the debounced level from the real pins so a button already held at
     * boot does not register as a fresh press on the first poll. */
    btn_start_stop.was_pressed = (gpio_get_level(btn_start_stop.pin) == 0);
    btn_speaker_mute.was_pressed = (gpio_get_level(btn_speaker_mute.pin) == 0);

    buttons_set_mic_muted(false);
    buttons_set_speaker_muted(false);

    ESP_LOGI(TAG, "Physical buttons ready (%d ms debounce)", BTN_DEBOUNCE_MS);
}

void buttons_poll(void)
{
    if (button_pressed_edge(&btn_start_stop)) {
        on_start_stop_pressed();
    }

    if (button_pressed_edge(&btn_speaker_mute)) {
        on_speaker_mute_pressed();
    }
}
