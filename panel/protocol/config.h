/**
 * @file config.h
 * UART link settings for the CrowPanel 2.8" ESP32 (display side).
 *
 * Scoped to the UART test harness. The display's own pins (SPI, touch,
 * backlight) are set in platformio.ini and include/display_driver.h and are
 * not repeated here.
 *
 * Included by panel/src/uart_link.cpp via a relative path (see the note at the
 * top of that file). The MCU side has its own copy of the link settings in
 * mcu/uno_q_mcu_uart_test/config.h -- the two must agree on baud and framing,
 * and nothing checks that for you.
 */

#pragma once

/* ------------------------------------------------------------------------
 * UART to the UNO Q MCU
 * ------------------------------------------------------------------------
 *
 * GPIO 16/17 are the CrowPanel's UART1 and are otherwise unused by this
 * project -- see the pin map in docs/panel-hardware.md. UART0 is the USB serial
 * console and must not be touched; UART2's default pins collide with the
 * display bus.
 */
#define DISPLAY_UART_NUM   1     /* HardwareSerial(1) */
#define DISPLAY_UART_RX    16    /* GPIO16 <- MCU TX (D1) */
#define DISPLAY_UART_TX    17    /* GPIO17 -> MCU RX (D0) */
#define DISPLAY_UART_BAUD  115200

/* ------------------------------------------------------------------------
 * Wire protocol
 * ------------------------------------------------------------------------ */

#define WIRE_PROTOCOL_VERSION 1
#define FIRMWARE_VERSION      "0.1.0"

/* Longest protocol line we will accept. Anything longer is a framing fault
 * (usually a missing newline on the far side), so it is dropped rather than
 * parsed. */
#define UART_BUFFER_SIZE   256
#define UART_TIMEOUT_MS    50    /* readStringUntil() timeout, not a link timeout */

/* ------------------------------------------------------------------------
 * Screen text the panel supplies itself
 * ------------------------------------------------------------------------ */

/*
 * Shown when the orchestrator reports {"t":"unclear"} -- below the confidence
 * threshold, or a recognised gloss with no row in phrases.json. That message
 * carries a confidence and no text, so the wording is the panel's to choose.
 *
 * It goes on the RESULT screen, not the error screen: "I did not understand"
 * is an answer, and the device is allowed to give it (docs/architecture.md
 * section 3). With the vocabulary unreconciled this is the COMMON outcome
 * today, not a rare one, so it has to read as a normal reply.
 */
#define UNCLEAR_TEXT  "Didn't catch that.\nPlease sign again."

/* ------------------------------------------------------------------------
 * Display geometry (from the existing UI -- informational)
 * ------------------------------------------------------------------------ */

#define DISPLAY_WIDTH   240      /* portrait canvas */
#define DISPLAY_HEIGHT  320

/* ------------------------------------------------------------------------
 * Buttons -- MIRROR ONLY
 * ------------------------------------------------------------------------
 *
 * The real definitions live in panel/include/buttons.h and that is the file to
 * edit if the harness is wired the other way round. These copies exist so code
 * that does not include buttons.h still has the numbers. Keep them in sync by
 * hand; nothing checks.
 */
#define BTN_START_STOP     32    /* GPIO_D IO32 */
#define BTN_SPEAKER_MUTE   25    /* GPIO_D IO25 */
