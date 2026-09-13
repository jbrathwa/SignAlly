/**
 * @file uart_link.cpp
 * UART link to the UNO Q MCU -- implementation. See uart_link.h.
 *
 * C++ because it uses HardwareSerial and Arduino String, the same reason
 * demo_manager is C++. The UI it drives is pure C and is reached through the
 * extern "C" block below.
 */

#include <Arduino.h>
#include <HardwareSerial.h>

/*
 * Reached by relative path rather than an -I entry: panel/protocol/config.h is
 * a generic enough name that putting that directory on the global include path
 * would shadow any framework header of the same name. uart_receiver.h pulls in
 * its own config.h, which resolves next to it.
 */
#include "../protocol/uart_receiver.h"
#include "uart_link.h"

extern "C" {
    #include "ui_center.h"
    #include "ui_status_bar.h"
    #include "buttons.h"
    #include "esp_log.h"
}

static const char *TAG = "uart_link";

/*
 * The wire's state ids and the UI's ui_state_t must agree. They are declared in
 * two files that know nothing about each other, so pin the mapping here: a
 * reordering of either enum becomes a build error instead of a wrong screen.
 */
static_assert((int)UART_STATE_BOOT      == (int)UI_STATE_BOOT,      "state enum drift: boot");
static_assert((int)UART_STATE_INTRO     == (int)UI_STATE_INTRO,     "state enum drift: intro");
static_assert((int)UART_STATE_LISTENING == (int)UI_STATE_LISTENING, "state enum drift: listening");
static_assert((int)UART_STATE_ANALYZING == (int)UI_STATE_ANALYZING, "state enum drift: analyzing");
static_assert((int)UART_STATE_RESULT    == (int)UI_STATE_RESULT,    "state enum drift: result");
static_assert((int)UART_STATE_ERROR     == (int)UI_STATE_ERROR,     "state enum drift: error");

static HardwareSerial uart(DISPLAY_UART_NUM);
static UARTReceiver   rx(uart);

static bool          link_open = false;
static bool          mcu_owns_state = false;
static unsigned long tx_seq = 1;   /* our counter, independent of the MCU's */

/* ------------------------------------------------------------------------
 * Outgoing
 * ------------------------------------------------------------------------ */

static void send(const String &msg)
{
    if (!link_open) return;
    uart.println(msg);
    ESP_LOGI(TAG, "[UART TX] %s", msg.c_str());
}

static void send_ack(unsigned long seq)
{
    send(uartBuildAck(seq));
}

/* ------------------------------------------------------------------------
 * Dispatch
 * ------------------------------------------------------------------------ */

static void apply_state(int state, unsigned long seq)
{
    if (state == UART_STATE_INVALID) {
        /* Deliberately not acked: a gap in the MCU's ack ledger is how an
         * unknown state name makes itself visible in the log. */
        ESP_LOGW(TAG, "unknown state name, dropped");
        return;
    }

    /* Leaving RESULT wipes the label, matching what the physical Start/Stop
     * handler does in buttons.c, so a stale sentence can never flash on the
     * next RESULT before its text arrives. */
    if (ui_get_state() == UI_STATE_RESULT && state != UART_STATE_RESULT) {
        ui_clear_result();
    }

    /* Keep the Start/Stop toggle honest when the screen moves without a press.
     * RESULT and ERROR are left alone: they arrive mid-capture and after a
     * failed start alike, so they say nothing about the flag. */
    if (state == UART_STATE_INTRO) {
        buttons_set_capture_active(false);
    } else if (state == UART_STATE_LISTENING || state == UART_STATE_ANALYZING) {
        buttons_set_capture_active(true);
    }

    mcu_owns_state = true;
    ui_set_state((ui_state_t)state);
    send_ack(seq);
}

static void apply_result(const uart_msg_t &m)
{
    /* Text first, then the screen. The other order shows the previous result
     * for one frame. */
    ui_show_result(m.text.c_str());
    mcu_owns_state = true;
    ui_set_state(UI_STATE_RESULT);

    ESP_LOGI(TAG, "result id=%s conf=%.2f text=\"%s\"",
             m.id.c_str(), m.conf, m.text.c_str());
    send_ack(m.seq);
}

/**
 * Status bar update. Unlike state and result this does NOT claim ownership of
 * the state machine -- battery and mute levels say nothing about which screen
 * should be up, and a panel running the standalone demo with no MCU driving it
 * should still be able to take a battery reading without the demo shutting off.
 *
 * The mute flags go through the buttons layer rather than ui_*_set_state()
 * directly, so the wire and the physical mute switch share one flag instead of
 * drifting apart -- the same routing the JSON demo state used to use.
 */
static void apply_status(const uart_msg_t &m)
{
    if (m.has_battery) ui_battery_set_value(m.battery);
    if (m.has_mic)     buttons_set_mic_muted(m.mic_muted);
    if (m.has_spk)     buttons_set_speaker_muted(m.spk_muted);

    /* "--" marks a field the message did not carry, so the log distinguishes
     * "set to false" from "left alone". */
    char bat[8];
    if (m.has_battery) snprintf(bat, sizeof bat, "%u%%", m.battery);
    else               snprintf(bat, sizeof bat, "--");

    ESP_LOGI(TAG, "status bat=%s mic_muted=%s spk_muted=%s", bat,
             m.has_mic ? (m.mic_muted ? "true" : "false") : "--",
             m.has_spk ? (m.spk_muted ? "true" : "false") : "--");

    send_ack(m.seq);
}

/**
 * "I did not understand that." A normal outcome, not a fault -- so it lands on
 * the RESULT screen with the panel's own wording rather than on ERROR. The
 * message carries a confidence and no text.
 */
static void apply_unclear(const uart_msg_t &m)
{
    /* Text first, then the screen -- same ordering rule as apply_result(), so
     * the previous sentence cannot show for a frame. */
    ui_show_result(UNCLEAR_TEXT);
    mcu_owns_state = true;
    ui_set_state(UI_STATE_RESULT);

    ESP_LOGI(TAG, "unclear conf=%.2f", m.conf);
    send_ack(m.seq);
}

/**
 * A fault worth showing: camera lost, nobody in frame, recognition offline.
 *
 * Unlike every other message here this one used to be log-only, which meant the
 * panel kept displaying a stale screen while the orchestrator believed it had
 * reported a problem. It now takes the screen and acks like the rest.
 *
 * There is no "error over" in the protocol -- the orchestrator clears the error
 * by re-sending a state message (core.py, _tracking), which apply_state()
 * already handles.
 */
static void apply_error(const uart_msg_t &m)
{
    ui_show_error(m.msg.c_str());
    mcu_owns_state = true;
    ui_set_state(UI_STATE_ERROR);

    ESP_LOGW(TAG, "error: %s", m.msg.c_str());
    send_ack(m.seq);
}

static void dispatch(const uart_msg_t &m)
{
    switch (m.type) {
        case UART_MSG_STATE:
            apply_state(m.state, m.seq);
            break;

        case UART_MSG_RESULT:
            apply_result(m);
            break;

        case UART_MSG_UNCLEAR:
            apply_unclear(m);
            break;

        case UART_MSG_STATUS:
            apply_status(m);
            break;

        case UART_MSG_HELLO:
            ESP_LOGI(TAG, "MCU hello received");
            send_ack(0);                  /* hello carries no seq of its own */
            break;

        case UART_MSG_ACK:
            ESP_LOGI(TAG, "MCU acked seq %lu", m.seq);
            break;

        case UART_MSG_ERROR:
            apply_error(m);
            break;

        case UART_MSG_BUTTON:
            /* Button events flow display -> MCU only. */
            ESP_LOGW(TAG, "unexpected button message from MCU");
            break;

        default:
            ESP_LOGW(TAG, "unhandled message type");
            break;
    }
}

/* ------------------------------------------------------------------------
 * Public API
 * ------------------------------------------------------------------------ */

void uart_link_init(void)
{
    uart.begin(DISPLAY_UART_BAUD, SERIAL_8N1, DISPLAY_UART_RX, DISPLAY_UART_TX);
    uart.setTimeout(UART_TIMEOUT_MS);
    link_open = true;

    ESP_LOGI(TAG, "UART%d up on RX=GPIO%d TX=GPIO%d @ %d 8N1",
             DISPLAY_UART_NUM, DISPLAY_UART_RX, DISPLAY_UART_TX,
             DISPLAY_UART_BAUD);

    /* Announce ourselves. If the MCU is not up yet nobody hears this; it also
     * sends its own hello whenever it boots, and neither side gates on the
     * other, so the order does not matter. */
    send(uartBuildHello());
}

void uart_link_poll(void)
{
    if (!link_open) return;

    String line;
    while (rx.readLine(line)) {
        ESP_LOGI(TAG, "[UART RX] %s", line.c_str());

        uart_msg_t m;
        if (!uartDecode(line, m)) {
            ESP_LOGW(TAG, "unknown message type, ignored");
            continue;
        }
        dispatch(m);
    }

    if (rx.overflowed()) {
        ESP_LOGW(TAG, "oversized line, resyncing to next newline");
    }
}

bool uart_link_owns_state(void)
{
    return mcu_owns_state;
}

void uart_link_report_button(const char *name, bool on)
{
    if (!link_open || name == NULL) return;
    send(uartBuildButton(tx_seq, name, on));
}
