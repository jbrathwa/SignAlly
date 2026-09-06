/**
 * @file uart_link.h
 * UART link to the UNO Q MCU -- the live integration for this PlatformIO build.
 *
 * The MCU owns the state machine; this module receives its decisions, applies
 * them to the existing LVGL UI, and acks. See docs/WIRE_PROTOCOL.md for the
 * schema and docs/STATE_MACHINE.md for who decides what.
 *
 * Pins, baud and buffer sizes come from panel/protocol/config.h; parsing comes
 * from panel/protocol/uart_receiver.h. Both are on the include path via
 * platformio.ini, and both are shared with the standalone reference sketch so
 * the two cannot drift.
 */

#ifndef UART_LINK_H
#define UART_LINK_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Open UART1 and announce ourselves to the MCU.
 * Call after ui_init() -- an inbound message can drive the UI on the very
 * first poll, so the widgets have to exist first.
 */
void uart_link_init(void);

/**
 * @brief Drain and dispatch any complete lines from the MCU.
 * Call once per loop() iteration. Non-blocking: it returns immediately when a
 * line is still in flight, so it will not stall LVGL.
 */
void uart_link_poll(void);

/**
 * @brief Whether the MCU has taken ownership of the state machine.
 *
 * False until the first state or result message arrives, true from then until
 * reboot. While true, src/capture_stub.c must not run -- both would drive
 * LISTENING -> ANALYZING -> RESULT and the stub's shorter timers would win,
 * painting its placeholder text over the real result. main.cpp gates the stub
 * on this.
 *
 * With the UART unplugged this never becomes true and the panel behaves
 * exactly as it did before the link existed.
 */
bool uart_link_owns_state(void);

/**
 * @brief Tell the MCU that a physical enclosure button was pressed.
 * @param name "start", "stop" or "mute".
 * @param on   For "mute" only: the state the press left the speaker in.
 *             Ignored for start/stop.
 *
 * Notification, not a request: the panel has already acted on the press
 * locally through buttons.c. Not acked. A no-op before uart_link_init().
 *
 * `on` is not optional decoration. The orchestrator reads a mute press as
 * `bool(msg.get("on", False))`, so a press reported without it registers
 * upstream as an UNMUTE however many times you press it.
 */
void uart_link_report_button(const char *name, bool on);

#ifdef __cplusplus
}
#endif

#endif /* UART_LINK_H */
