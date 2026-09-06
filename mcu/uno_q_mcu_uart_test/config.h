/**
 * @file config.h
 * Pin map and link settings for the UNO Q MCU side of the SignAlly UART test.
 *
 * Everything the wiring depends on lives here. If the link does not come up,
 * this is the only file you should need to edit.
 */

#pragma once

/* ------------------------------------------------------------------------
 * UART to the CrowPanel display
 * ------------------------------------------------------------------------ */

/* Hardware UART port. Serial1 on an UNO-form-factor board is normally already
 * routed to D0/D1, so begin() needs no pin arguments.
 * ** Verify with your UNO Q docs. ** */
#define MCU_UART        Serial1
#define MCU_UART_BAUD   115200

/* Physical pins carrying that port.
 *
 * These two are DOCUMENTATION on most variants -- Serial1 is hardwired to them
 * and MCU_UART.begin() ignores any pins you pass. They are here so the wiring
 * table has a single source of truth, and so a variant that *does* need
 * explicit pins has somewhere to get them (see MCU_UART_NEEDS_EXPLICIT_PINS).
 *
 * ** Verify with your UNO Q docs before powering up. **
 */
#define MCU_RX_PIN      0    /* D0 <- CrowPanel TX (GPIO 17). Verify. */
#define MCU_TX_PIN      1    /* D1 -> CrowPanel RX (GPIO 16). Verify. */

/* Set to 1 only if your core's Serial1.begin() takes (baud, config, rx, tx).
 * Leave at 0 for stock UNO-form-factor cores. */
#define MCU_UART_NEEDS_EXPLICIT_PINS 0

/* USB serial back to the laptop -- debug log and manual command input.
 * Kept strictly separate from MCU_UART: never print protocol JSON to this port
 * or debug text to that one. */
#define DEBUG_SERIAL      Serial
#define DEBUG_SERIAL_BAUD 115200

/* ------------------------------------------------------------------------
 * Wire protocol
 * ------------------------------------------------------------------------ */

#define WIRE_PROTOCOL_VERSION 1
#define MCU_FW_VERSION        "0.1.0"

/* Longest line we will accept from the display before giving up on it. Guards
 * against a stuck line holding readStringUntil() open. */
#define UART_LINE_MAX_LEN   192
#define UART_READ_TIMEOUT_MS 50

/* How long to wait for an ack before complaining to the debug log. Advisory
 * only -- nothing is retransmitted. */
#define UART_ACK_TIMEOUT_MS 5000

/* ------------------------------------------------------------------------
 * Mock pipeline timing (stands in for MediaPipe + the LSTM on the Linux side)
 * ------------------------------------------------------------------------ */

#define LISTENING_DURATION_MS 3000   /* LISTENING -> ANALYZING */
#define ANALYZING_DURATION_MS 2000   /* ANALYZING -> RESULT    */

/* The canned result pushed at the end of the mock run. */
#define MOCK_RESULT_ID   "pain"
#define MOCK_RESULT_TEXT "I have pain"
#define MOCK_RESULT_CONF 0.92f

/* Sent by the "error" command. The orchestrator's real ones come from
 * TRACKING_TEXT and the offline/camera paths in core.py: "Move back",
 * "Nobody in frame", "Recognition offline", "Camera not found". */
#define MOCK_ERROR_TEXT "Nobody in frame"

/* Sent by the "unclear" command. With the vocabulary unreconciled -- 17 rows in
 * phrases.json against a 262-class head -- this is the COMMON outcome on real
 * signing today, not a rare one, so it is worth exercising deliberately. */
#define MOCK_UNCLEAR_CONF 0.35f

/* Mock status-bar values, stood up the same way MOCK_RESULT_* is. The UNO Q has
 * no fuel gauge and no audio path in this harness, so these are the starting
 * values the MCU pushes to the display once the link is up; change them live
 * with "bat <n>", "mic mute" and "spk mute" on the Serial Monitor. */
#define MOCK_BATTERY_PCT 87
#define MOCK_MIC_MUTED   false
#define MOCK_SPK_MUTED   false

/* ------------------------------------------------------------------------
 * Physical buttons
 * ------------------------------------------------------------------------
 *
 * MIRROR ONLY -- these buttons are wired to the CrowPanel's GPIO_D header, not
 * to the UNO Q, and the ESP32 owns them (include/buttons.h is the real source
 * of truth). They are repeated here so MCU-side code that reasons about
 * incoming {"t":"button"} messages has the numbers. Changing them here changes
 * nothing on the hardware.
 */
#define BTN_START_STOP_GPIO   32   /* GPIO_D IO32, breadboard row 5 */
#define BTN_SPEAKER_MUTE_GPIO 25   /* GPIO_D IO25, breadboard row 7 */
