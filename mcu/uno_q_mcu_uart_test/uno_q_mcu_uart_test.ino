/**
 * @file uno_q_mcu_uart_test.ino
 * SignAlly UART test -- Arduino UNO Q (STM32 MCU side).
 *
 * Owns the state machine. Everything the CrowPanel displays is decided here and
 * pushed over Serial1 as one JSON line per message; the panel only renders and
 * acks. See docs/WIRE_PROTOCOL.md and docs/STATE_MACHINE.md.
 *
 * Two serial ports, kept strictly apart:
 *   Serial1 (D0/D1) -- protocol JSON to/from the display. Nothing else.
 *   Serial  (USB)   -- human-readable debug log, and manual commands in.
 * Mixing them is the fastest way to make the display's parser choke on a log
 * line, so every print in this file goes through DEBUG_SERIAL or MCU_UART
 * deliberately, never through a bare Serial.
 *
 * Commands are typed into the Serial Monitor at 115200: "start", "stop",
 * "unclear", "error" and the status-bar commands. Nothing else drives it.
 */

#include <Arduino.h>
#include "config.h"
#include "wire_protocol.h"

/*
 * There is no Bridge in this sketch, deliberately.
 *
 * An earlier revision carried a disabled `USE_ARDUINO_BRIDGE` block that
 * guessed at the API: header <ArduinoBridge.h>, and a Bridge.get/put key-value
 * poll. Both are wrong. The real one is <Arduino_RouterBridge.h> with an RPC
 * shape -- Bridge.provide/provide_safe on this side, Bridge.call/notify from
 * Python -- as verified by the applab-bridgetest spike. Dead code that names a
 * non-existent header is worse than no code, so it is gone.
 *
 * The Bridge belongs to the OTHER sketch: applab/signally-console/sketch/, which is
 * what you flash when the orchestrator is driving. This one is the bench
 * harness and is driven by typing at the Serial Monitor.
 */

/* ------------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------------ */

static unsigned long seq = 1;              /* our outgoing sequence counter */
static unsigned long last_sent_seq = 0;    /* seq awaiting an ack, 0 = none  */
static unsigned long last_sent_at_ms = 0;
static bool ack_timeout_reported = false;

/* "idle" is the orchestrator's resting screen and its STOP target. This
 * harness stands in for the orchestrator, so it uses the orchestrator's
 * vocabulary -- the panel maps idle onto its INTRO screen. */
static String current_state = "idle";
static unsigned long state_start_time = 0;

/*
 * Set the first time we hear anything at all from the display -- its hello, or
 * an ack for something of ours. Either proves the far side is listening. We do
 * not gate sending on it, so a panel that boots late still catches up; it only
 * decides when the opening status push goes out.
 */
static bool display_seen = false;

/*
 * The status bar the display renders. The MCU owns these outright -- the panel
 * has no source for them of its own any more (demo_state.json no longer seeds
 * anything), so until the push below lands the bar sits at its built-in
 * defaults. That is deliberate: a silent link now looks silent.
 */
static uint8_t battery_pct = MOCK_BATTERY_PCT;
static bool    mic_muted   = MOCK_MIC_MUTED;
static bool    spk_muted   = MOCK_SPK_MUTED;
static bool    status_push_pending = true;   /* cleared by the opening push */

/* Forward declarations */
static void sendStateMessage(const String &state);
static void sendResultMessage(const String &id, const String &text, float conf);
static void sendStatusMessage(void);
static void sendUnclearMessage(float conf);
static void sendErrorMessage(const String &text);
static void parseUARTMessage(const String &msg);
static void setDisplayState(const String &command);
static void pollDebugSerial(void);
static void pollUART(void);
static void mockPipelineTick(void);

/* ------------------------------------------------------------------------
 * Setup / loop
 * ------------------------------------------------------------------------ */

void setup()
{
    DEBUG_SERIAL.begin(DEBUG_SERIAL_BAUD);

#if MCU_UART_NEEDS_EXPLICIT_PINS
    MCU_UART.begin(MCU_UART_BAUD, SERIAL_8N1, MCU_RX_PIN, MCU_TX_PIN);
#else
    MCU_UART.begin(MCU_UART_BAUD);
#endif
    MCU_UART.setTimeout(UART_READ_TIMEOUT_MS);

    /* Give the USB CDC a moment to enumerate so the first lines are not lost
     * to a Serial Monitor that has not attached yet. */
    delay(1000);

    DEBUG_SERIAL.println();
    DEBUG_SERIAL.println("[MCU] bench harness up");
    DEBUG_SERIAL.print("[MCU] fw ");
    DEBUG_SERIAL.print(MCU_FW_VERSION);
    DEBUG_SERIAL.print(", protocol v");
    DEBUG_SERIAL.print(WIRE_PROTOCOL_VERSION);
    DEBUG_SERIAL.print(", UART ");
    DEBUG_SERIAL.print(MCU_UART_BAUD);
    DEBUG_SERIAL.println(" 8N1");
    DEBUG_SERIAL.println("[MCU] type: start | stop | error | unclear | state <name> | ?");
    DEBUG_SERIAL.println("[MCU]       status | bat <0-100> | mic mute|unmute | spk mute|unmute");

    /* Announce ourselves. The display answers with an ack carrying seq 0, and
     * sends its own hello whenever it finishes booting -- the two are
     * independent, so ordering between them does not matter. */
    const String hello = createHelloMessage(WIRE_PROTOCOL_VERSION, MCU_FW_VERSION);
    MCU_UART.println(hello);
    DEBUG_SERIAL.print("[UART TX] ");
    DEBUG_SERIAL.println(hello);

    current_state = "idle";
    state_start_time = millis();
}

void loop()
{
    pollDebugSerial();   /* the same commands, typed into the Serial Monitor */
    pollUART();          /* acks and button events from the display */
    mockPipelineTick();  /* stands in for MediaPipe + the LSTM */

    /* Opening status push. Deferred out of setup() because the display may not
     * have been powered yet then -- we wait until it has proved it is listening
     * rather than shouting the status bar into an unconnected wire. Sent from
     * the loop, not from inside the parser that sets display_seen, to keep
     * transmit out of the receive path. */
    if (status_push_pending && display_seen) {
        status_push_pending = false;
        DEBUG_SERIAL.println("[MCU] display is up -- pushing initial status bar");
        sendStatusMessage();
    }

    delay(5);
}

/* ------------------------------------------------------------------------
 * Outgoing
 * ------------------------------------------------------------------------ */

/** Send one protocol line and start the ack clock for it. */
static void sendLine(const String &msg, unsigned long msg_seq)
{
    MCU_UART.println(msg);
    DEBUG_SERIAL.print("[UART TX] ");
    DEBUG_SERIAL.println(msg);

    last_sent_seq = msg_seq;
    last_sent_at_ms = millis();
    ack_timeout_reported = false;
}

static void sendStateMessage(const String &state)
{
    const unsigned long this_seq = seq;   /* createStateMessage consumes it */
    const String msg = createStateMessage(seq, state);

    current_state = state;
    state_start_time = millis();

    sendLine(msg, this_seq);
}

static void sendResultMessage(const String &id, const String &text, float conf)
{
    const unsigned long this_seq = seq;
    const String msg = createResultMessage(seq, id, text, conf);

    /* The display switches to RESULT on this message, so track it as the
     * current state or mockPipelineTick() would keep re-firing the ANALYZING
     * timer forever. */
    current_state = "result";
    state_start_time = millis();

    sendLine(msg, this_seq);
}

/**
 * Push the whole status bar. Cheap enough (one line, ~55 bytes) that there is
 * no case for sending partial updates from here, even though the display
 * accepts them.
 */
static void sendStatusMessage(void)
{
    const unsigned long this_seq = seq;
    const String msg = createStatusMessage(seq, battery_pct, mic_muted, spk_muted);
    sendLine(msg, this_seq);
}

static void sendUnclearMessage(float conf)
{
    const unsigned long this_seq = seq;
    const String msg = createUnclearMessage(seq, conf);

    /* The panel lands on RESULT for this, so track it as such or the ANALYZING
     * timer below would re-fire forever -- the same reason sendResultMessage()
     * sets current_state. */
    current_state = "result";
    state_start_time = millis();

    sendLine(msg, this_seq);
}

static void sendErrorMessage(const String &text)
{
    const unsigned long this_seq = seq;
    const String msg = createErrorMessage(seq, text);

    /* There is no "error over" in the protocol: the orchestrator clears the
     * error screen by sending a state message. Park in a state the mock
     * pipeline will not drive out from under it. */
    current_state = "error";
    state_start_time = millis();

    sendLine(msg, this_seq);
}

/* ------------------------------------------------------------------------
 * Incoming -- from the display
 * ------------------------------------------------------------------------ */

static void pollUART(void)
{
    while (MCU_UART.available()) {
        String incoming = MCU_UART.readStringUntil('\n');
        incoming.trim();                       /* drops the CR from println() */
        if (incoming.length() == 0) continue;

        if (incoming.length() > UART_LINE_MAX_LEN) {
            DEBUG_SERIAL.println("[UART RX] line too long, dropped");
            continue;
        }

        DEBUG_SERIAL.print("[UART RX] ");
        DEBUG_SERIAL.println(incoming);
        parseUARTMessage(incoming);
    }

    /* Advisory ack timeout. Nothing is retransmitted -- this exists so a dead
     * link shows up in the log instead of as a display that quietly stopped
     * changing. */
    if (last_sent_seq != 0 && !ack_timeout_reported &&
        (millis() - last_sent_at_ms) > UART_ACK_TIMEOUT_MS) {
        DEBUG_SERIAL.print("[WARN] no ack for seq ");
        DEBUG_SERIAL.print(last_sent_seq);
        DEBUG_SERIAL.println(" -- check RX/TX polarity and GND");
        ack_timeout_reported = true;
    }
}

static void parseUARTMessage(const String &msg)
{
    unsigned long acked = 0;
    String s;

    /* Anything arriving on this port means the panel is powered and listening.
     * Set here rather than only in the hello branch: the display sends its
     * hello once at its own boot, which we miss entirely whenever it powers up
     * before we do -- the common case on a shared supply. An ack proves the
     * same thing. */
    display_seen = true;

    if (parseJSON_ack(msg, acked)) {
        if (acked == last_sent_seq) {
            DEBUG_SERIAL.println("[ACK] message confirmed by display");
            last_sent_seq = 0;
        } else {
            /* Not fatal: the ack for hello carries seq 0, and a late ack for an
             * older message can arrive after we have already moved on. */
            DEBUG_SERIAL.print("[ACK] seq ");
            DEBUG_SERIAL.print(acked);
            DEBUG_SERIAL.print(" (awaiting ");
            DEBUG_SERIAL.print(last_sent_seq);
            DEBUG_SERIAL.println(")");
        }
        return;
    }

    if (parseJSON_button(msg, s)) {
        DEBUG_SERIAL.print("[BUTTON] physical button on display: ");
        DEBUG_SERIAL.println(s);
        /* The panel has already acted on its own button locally. Mirror the
         * state here so the mock pipeline stays in step with the screen. */
        if (s == "start") {
            current_state = "listening";
            state_start_time = millis();
        } else if (s == "stop") {
            current_state = "idle";
            state_start_time = millis();
        } else if (s == "mute") {
            /* The panel reports the flag its press produced, not a bare
             * toggle, so mirror the value rather than inverting ours -- an
             * inversion would drift permanently the moment one message is
             * dropped. Same field the orchestrator reads. Nothing is sent back:
             * the screen is already right, and echoing would fight a second
             * press racing the reply. */
            bool on = spk_muted;
            if (wireGetBool(msg, "on", on)) {
                spk_muted = on;
            } else {
                DEBUG_SERIAL.println("[BUTTON] mute with no 'on' field -- old panel firmware?");
                spk_muted = !spk_muted;
            }
            DEBUG_SERIAL.print("[BUTTON] speaker now ");
            DEBUG_SERIAL.println(spk_muted ? "MUTED" : "UNMUTED");
        }
        return;
    }

    if (parseJSON_hello(msg, s)) {
        DEBUG_SERIAL.print("[HELLO] display up, fw ");
        DEBUG_SERIAL.println(s);

        /* A hello means the panel has just (re)booted, so its status bar is
         * back at the built-in defaults and whatever we pushed before is gone.
         * Re-arm the push rather than leaving it stale for the rest of the
         * session -- this is the only signal we get that the screen was
         * cleared underneath us. */
        status_push_pending = true;

        const String ack = createAckMessage(0);
        MCU_UART.println(ack);
        DEBUG_SERIAL.print("[UART TX] ");
        DEBUG_SERIAL.println(ack);
        return;
    }

    if (wireIsType(msg, "error")) {
        String em = "?";
        wireGetString(msg, "msg", em);
        DEBUG_SERIAL.print("[ERROR] from display: ");
        DEBUG_SERIAL.println(em);
        return;
    }

    DEBUG_SERIAL.println("[UART] unknown message type, ignored");
}

/* ------------------------------------------------------------------------
 * Incoming -- commands from App Lab or the Serial Monitor
 * ------------------------------------------------------------------------ */

/**
 * The single entry point for "the operator asked for X". Kept as one function
 * so every command path lands in the same place and they cannot drift.
 */
static void setDisplayState(const String &command)
{
    String cmd = command;
    cmd.trim();
    cmd.toLowerCase();

    if (cmd == "start" || cmd == "listening") {
        DEBUG_SERIAL.println("[MCU] setDisplayState(listening) called");
        sendStateMessage("listening");

    } else if (cmd == "stop" || cmd == "idle" || cmd == "intro") {
        DEBUG_SERIAL.println("[MCU] setDisplayState(idle) called");
        sendStateMessage("idle");

    } else if (cmd == "error") {
        /* A payload message with real text, which is what the orchestrator
         * emits -- not {"t":"state","s":"error"}. The panel shows the text. */
        sendErrorMessage(MOCK_ERROR_TEXT);

    } else if (cmd == "unclear") {
        sendUnclearMessage(MOCK_UNCLEAR_CONF);

    } else if (cmd == "analyzing") {
        /* Exposed for poking at a single transition by hand. The mock pipeline
         * reaches this on its own from "start". */
        sendStateMessage("analyzing");

    } else if (cmd == "result") {
        sendResultMessage(MOCK_RESULT_ID, MOCK_RESULT_TEXT, MOCK_RESULT_CONF);

    } else if (cmd == "status") {
        sendStatusMessage();

    } else if (cmd.startsWith("bat ")) {
        const long v = cmd.substring(4).toInt();
        if (v < 0 || v > 100) {
            DEBUG_SERIAL.println("[MCU] battery must be 0..100");
        } else {
            battery_pct = (uint8_t)v;
            sendStatusMessage();
        }

    } else if (cmd == "mic mute" || cmd == "mic unmute") {
        mic_muted = cmd.endsWith("unmute") ? false : true;
        sendStatusMessage();

    } else if (cmd == "spk mute" || cmd == "spk unmute") {
        spk_muted = cmd.endsWith("unmute") ? false : true;
        sendStatusMessage();

    } else if (cmd == "?" || cmd == "help") {
        DEBUG_SERIAL.println("[MCU] commands: start | stop | error | unclear | analyzing | result");
        DEBUG_SERIAL.println("[MCU]           status | bat <0-100> | mic mute|unmute | spk mute|unmute");
        DEBUG_SERIAL.print("[MCU] current state: ");
        DEBUG_SERIAL.println(current_state);
        DEBUG_SERIAL.print("[MCU] status bar: bat ");
        DEBUG_SERIAL.print(battery_pct);
        DEBUG_SERIAL.print("%, mic ");
        DEBUG_SERIAL.print(mic_muted ? "MUTED" : "unmuted");
        DEBUG_SERIAL.print(", spk ");
        DEBUG_SERIAL.println(spk_muted ? "MUTED" : "unmuted");

    } else {
        DEBUG_SERIAL.print("[MCU] Unknown command: ");
        DEBUG_SERIAL.println(command);
    }
}

static void pollDebugSerial(void)
{
    if (!DEBUG_SERIAL.available()) return;

    String line = DEBUG_SERIAL.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    /* "state <name>" is the long form; bare words are handled directly. */
    if (line.startsWith("state ")) {
        setDisplayState(line.substring(6));
    } else {
        setDisplayState(line);
    }
}

/* ------------------------------------------------------------------------
 * Mock pipeline
 *
 * TEMPORARY. Replaces MediaPipe landmark capture and the LSTM inference call
 * with two fixed delays, so the wire and the display can be validated before
 * either exists. Delete this function once the Linux side is real.
 * ------------------------------------------------------------------------ */

static void mockPipelineTick(void)
{
    const unsigned long elapsed = millis() - state_start_time;

    if (current_state == "listening" && elapsed >= LISTENING_DURATION_MS) {
        DEBUG_SERIAL.println("[MOCK] capture complete -> analyzing");
        sendStateMessage("analyzing");

    } else if (current_state == "analyzing" && elapsed >= ANALYZING_DURATION_MS) {
        DEBUG_SERIAL.println("[MOCK] inference complete -> result");
        sendResultMessage(MOCK_RESULT_ID, MOCK_RESULT_TEXT, MOCK_RESULT_CONF);
    }

    /* "idle", "result" and "error" are terminal here -- they wait for the
     * operator (App Lab, or a physical button) rather than a timer. */
}
