/**
 * @file signally_panel_relay.ino
 * SignAlly -- the STM32 hop between App Lab and the CrowPanel.
 *
 *   orchestrator (Linux :9977)
 *        |  SSE /events, POST /uplink
 *   signally-console (App Lab container)
 *        |  Bridge RPC          <-- this file is the other end
 *   THIS SKETCH (STM32)
 *        |  UART 115200 8N1, D0/D1
 *   panel/ (CrowPanel ESP32)
 *
 * ---------------------------------------------------------------------------
 * THIS SKETCH HAS NO OPINIONS.
 *
 * It does not parse the protocol, does not know what a state is, and above all
 * does NOT mint `seq`. It moves whole lines between a socket and a UART and
 * that is the entire job. The orchestrator owns device state and numbering;
 * two components stamping `seq` would be worse than having no panel at all
 * (docs/architecture.md).
 *
 * If you find yourself adding a JSON field here, the boundary has been drawn
 * wrong -- the change belongs in orchestrator/src/orchestrator/core.py.
 *
 * For bench work with no Linux stack running, use the OTHER sketch:
 * mcu/uno_q_mcu_uart_test/, which does have a mock state machine and drives the
 * panel from typed commands. The two are alternatives; only one can be flashed
 * at a time.
 * ---------------------------------------------------------------------------
 *
 * Logging goes to Monitor, not Serial. Monitor is App Lab's own channel; core
 * Arduino Serial has no precedent in the App Lab examples and the USB port is
 * App Lab's when it deploys.
 */

#include <Arduino_RouterBridge.h>
#include "config.h"

/* ------------------------------------------------------------------------
 * Downlink: Python -> here -> panel
 * ------------------------------------------------------------------------ */

/**
 * One protocol line from the orchestrator, forwarded verbatim.
 *
 * Registered with provide_safe() rather than provide(): the thread-safe form is
 * served in the main loop thread, which is the same thread that reads MCU_UART
 * below. The unsafe form runs in the Bridge's own update thread and would have
 * two threads on one UART with no lock between them.
 *
 * ** If downlink never arrives on the panel, try provide() here first. **
 * provide() is the form the applab-bridgetest spike verified on this board;
 * provide_safe() is documented in the Arduino_RouterBridge README but has not
 * been exercised here. Should you have to fall back, give this function its own
 * outbound queue that loop() drains -- do not let it touch MCU_UART directly.
 */
void onDisplayLine(String line) {
    MCU_UART.println(line);

    Monitor.print("[DOWN] ");
    Monitor.println(line);
}

/**
 * Liveness probe. The console calls this once per (re)connect to find out
 * whether a relay is on the other end; if it answers, the console stops acking
 * on the panel's behalf and lets the real acks come back up the wire.
 */
String onPanelHello() {
    return String(RELAY_FW_VERSION);
}

/* ------------------------------------------------------------------------
 * Uplink: panel -> here -> Python
 * ------------------------------------------------------------------------ */

static char    rx_buf[UART_LINE_MAX];
static size_t  rx_len = 0;
static bool    rx_overflowed = false;

/**
 * Drain whatever the panel has sent, one whole line at a time.
 *
 * Byte-at-a-time rather than readStringUntil(), which blocks for its timeout
 * whenever a line is still in flight. This must never stall: the same loop is
 * the one servicing downlink.
 */
static void pumpUplink() {
    while (MCU_UART.available()) {
        const char c = (char)MCU_UART.read();

        if (c == '\r') continue;

        if (c != '\n') {
            if (rx_len < UART_LINE_MAX - 1) {
                rx_buf[rx_len++] = c;
            } else {
                /* Runaway line. Drop it and resync on the next newline rather
                 * than forwarding a truncated object that the orchestrator's
                 * json.loads would reject anyway -- but noisily, in a place
                 * that cannot see the UART. */
                rx_overflowed = true;
            }
            continue;
        }

        /* Newline: the line is complete. */
        const size_t len = rx_len;
        rx_buf[rx_len] = '\0';
        rx_len = 0;

        if (rx_overflowed) {
            rx_overflowed = false;
            Monitor.println("[UP] oversized line dropped, resynced");
            continue;
        }
        if (len == 0) continue;

        /*
         * notify(), not call(): nothing comes back and we must not block the
         * loop waiting for the Linux side. Sent from loop() and never from
         * inside an RPC callback -- the library warns that calling out from
         * within a callback can deadlock the MCU-CPU IPC.
         */
        Bridge.notify(RPC_PANEL_UPLINK, String(rx_buf));

        Monitor.print("[UP] ");
        Monitor.println(rx_buf);
    }
}

/* ------------------------------------------------------------------------
 * Setup / loop
 * ------------------------------------------------------------------------ */

void setup() {
    Monitor.begin(115200);
    Bridge.begin();

    MCU_UART.begin(MCU_UART_BAUD);

    Bridge.provide_safe(RPC_DISPLAY_LINE, onDisplayLine);
    Bridge.provide_safe(RPC_PANEL_HELLO, onPanelHello);

    Monitor.print("[relay] ");
    Monitor.print(RELAY_FW_VERSION);
    Monitor.print(" up, UART ");
    Monitor.print(MCU_UART_BAUD);
    Monitor.println(" 8N1 on D0/D1");

    /*
     * No hello is sent to the panel from here. The relay is not a protocol
     * participant -- the orchestrator sends its own hello when the console
     * subscribes, and that one is forwarded like any other line. The panel
     * sends its hello whenever it boots and it travels up the same way.
     */
}

void loop() {
    pumpUplink();
    delay(2);
}
