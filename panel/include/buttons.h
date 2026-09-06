/**
 * @file buttons.h
 * Physical Button Input Layer for the Elecrow CrowPanel 2.8" enclosure
 *
 * The enclosure carries three physical controls:
 *   - Start/Stop momentary push button        -> handled here
 *   - Speaker mute/unmute push button (top right) -> handled here
 *   - Power slide switch                      -> cuts power, no firmware
 *
 * Note there is NO physical mic button. The mic mute flag exists and is
 * maintained, but only the backend or the demo JSON can move it.
 *
 * This module owns the mute flags for the whole project. Anything that needs
 * to know or change mute state goes through buttons_*_muted() rather than
 * calling ui_mic_set_state()/ui_speaker_set_state() directly, so the flags and
 * the status bar icons can never disagree.
 */

#ifndef BUTTONS_H
#define BUTTONS_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * GPIO wiring. Both buttons are expected to be wired button-to-GND and rely on
 * the ESP32 internal pull-up, so an idle input reads HIGH and a press reads LOW.
 *
 * GPIO 25 and 32 are the CrowPanel's `GPIO_D` expansion connector and the ONLY
 * user-available pins on this board -- everything else is committed by
 * Elecrow's design: 12/13/14/15/2/27 to the display, 33 to touch, 23/19/18/5
 * to the SD slot, 22/21 to I2C, 16/17 to UART1, 0 to the BOOT button.
 *
 * So these are not a free choice; the only open question is which button is on
 * which pin. If the harness turns out to be the other way round, swap the two
 * values here -- nothing else needs to change.
 */
#define BTN_START_STOP_GPIO     32
#define BTN_SPEAKER_MUTE_GPIO   25   /* top-right push button on the enclosure */

/*
 * Debounce window, applied at the GPIO read level. A press edge is accepted
 * immediately and further edges on that pin are ignored for this long, so the
 * response is instant and the contact bounce (and any double-tap from a stiff
 * enclosure button) is swallowed afterwards rather than before.
 */
#define BTN_DEBOUNCE_MS       250

/**
 * @brief Configure the button GPIOs and seed the mute flags to unmuted.
 * Call after ui_init() -- the handlers touch UI objects.
 */
void buttons_init(void);

/**
 * @brief Sample the buttons and dispatch handlers. Call from the main loop.
 * Runs in task context, not an ISR, so it is safe to call LVGL from here.
 */
void buttons_poll(void);

/**
 * @brief Current mic mute flag.
 */
bool buttons_mic_is_muted(void);

/**
 * @brief Current speaker mute flag.
 */
bool buttons_speaker_is_muted(void);

/**
 * @brief Set the mic mute flag and refresh the status bar icon.
 * There is no physical mic button; this exists for the backend and the JSON
 * demo state to drive the same flag the speaker button uses.
 */
void buttons_set_mic_muted(bool muted);

/**
 * @brief Set the speaker mute flag and refresh the status bar icon.
 * Driven by the top-right push button on the enclosure.
 */
void buttons_set_speaker_muted(bool muted);

/**
 * @brief Whether the device is actively trying to capture/watch for a sign.
 * This is NOT overall device power state -- that is a separate concern for a
 * future power-module pass. Read-only here: on_start_stop_pressed() in
 * buttons.c is the sole owner of every transition of this flag, per-screen
 * (see its switch on ui_get_state()) rather than a single unconditional
 * action, so there is deliberately no public setter.
 */
bool buttons_capture_is_active(void);

#ifdef __cplusplus
}
#endif

#endif /* BUTTONS_H */
