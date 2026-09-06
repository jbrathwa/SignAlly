/**
 * @file uart_receiver.h
 * Line framing, parsing and message building for the display side of the
 * SignAlly wire protocol.
 *
 * Header-only and hardware-agnostic: it takes a Stream, so the same code backs
 * the standalone reference sketch and the PlatformIO integration in
 * src/uart_link.cpp. Nothing here touches LVGL -- the caller decides what to do
 * with a parsed message.
 *
 * See docs/WIRE_PROTOCOL.md for the schema and the known limits of the
 * substring-scan parsing used here.
 */

#pragma once
#include <Arduino.h>
#include "config.h"

/* ------------------------------------------------------------------------
 * Message model
 * ------------------------------------------------------------------------ */

enum uart_msg_type_t {
    UART_MSG_UNKNOWN = 0,
    UART_MSG_HELLO,
    UART_MSG_STATE,
    UART_MSG_RESULT,
    UART_MSG_UNCLEAR,
    UART_MSG_STATUS,
    UART_MSG_ACK,
    UART_MSG_BUTTON,
    UART_MSG_ERROR
};

/**
 * State ids as they appear on the wire, in the same order as ui_state_t in
 * include/ui_center.h. uart_link.cpp static_asserts that the two agree, so a
 * reordering of the UI enum is a build error rather than a wrong screen.
 */
enum uart_state_id_t {
    UART_STATE_BOOT = 0,
    UART_STATE_INTRO,
    UART_STATE_LISTENING,
    UART_STATE_ANALYZING,
    UART_STATE_RESULT,
    UART_STATE_ERROR,
    UART_STATE_INVALID = -1
};

/** One decoded protocol line. Fields not carried by the type are left empty. */
struct uart_msg_t {
    uart_msg_type_t type = UART_MSG_UNKNOWN;
    unsigned long   seq  = 0;
    bool            has_seq = false;

    int    state = UART_STATE_INVALID;  /* state       */
    String text;                        /* result      */
    String id;                          /* result      */
    float  conf = 0.0f;                 /* result      */
    String button;                      /* button      */
    String msg;                         /* error       */

    /*
     * status. Every field is optional and carries its own has_* flag, so a
     * sender can push just the battery, or just a mute change, without having
     * to restate values it does not own. A missing field means "leave it
     * alone", never "set it to zero/false".
     */
    uint8_t battery    = 0;
    bool    has_battery = false;
    bool    mic_muted   = false;
    bool    has_mic     = false;
    bool    spk_muted   = false;
    bool    has_spk     = false;
};

/* ------------------------------------------------------------------------
 * Field extraction
 * ------------------------------------------------------------------------ */

/** @brief Type tag test: does this line carry "t":"<type>"? */
inline bool uartIsType(const String &json, const char *type)
{
    return json.indexOf(String("\"t\":\"") + type + "\"") >= 0;
}

/**
 * @brief Pull a numeric field out of a flat object.
 * @return true if the key was present and parsed.
 */
inline bool uartGetULong(const String &json, const char *key, unsigned long &out)
{
    const String needle = String("\"") + key + "\":";
    const int at = json.indexOf(needle);
    if (at < 0) return false;

    int i = at + needle.length();
    while (i < (int)json.length() && json[i] == ' ') i++;

    const int start = i;
    while (i < (int)json.length() && isDigit(json[i])) i++;
    if (i == start) return false;

    out = json.substring(start, i).toInt();
    return true;
}

/** @brief Pull a float field (e.g. "conf") out of a flat object. */
inline bool uartGetFloat(const String &json, const char *key, float &out)
{
    const String needle = String("\"") + key + "\":";
    const int at = json.indexOf(needle);
    if (at < 0) return false;

    int i = at + needle.length();
    while (i < (int)json.length() && json[i] == ' ') i++;

    const int start = i;
    while (i < (int)json.length() &&
           (isDigit(json[i]) || json[i] == '.' || json[i] == '-')) i++;
    if (i == start) return false;

    out = json.substring(start, i).toFloat();
    return true;
}

/**
 * @brief Pull a JSON boolean field out of a flat object.
 *
 * Accepts only the bare literals `true` and `false`; a quoted "true" is not a
 * boolean and is rejected, so a sender that stringifies its flags fails loudly
 * here instead of silently reading as false.
 *
 * @return true if the key was present and held a valid boolean.
 */
inline bool uartGetBool(const String &json, const char *key, bool &out)
{
    const String needle = String("\"") + key + "\":";
    const int at = json.indexOf(needle);
    if (at < 0) return false;

    int i = at + needle.length();
    while (i < (int)json.length() && json[i] == ' ') i++;

    if (json.startsWith("true", i))  { out = true;  return true; }
    if (json.startsWith("false", i)) { out = false; return true; }
    return false;
}

/**
 * @brief Pull a string field out of a flat object.
 *
 * No escape handling -- the value ends at the first '"'. A value containing a
 * quote or backslash comes back truncated; keep wire strings plain.
 */
inline bool uartGetString(const String &json, const char *key, String &out)
{
    const String needle = String("\"") + key + "\":\"";
    const int at = json.indexOf(needle);
    if (at < 0) return false;

    const int start = at + needle.length();
    const int end = json.indexOf('"', start);
    if (end < start) return false;

    out = json.substring(start, end);
    return true;
}

/** @brief Map a wire state name to its uart_state_id_t, or UART_STATE_INVALID. */
inline int uartParseStateName(const String &name)
{
    if (name == "boot")      return UART_STATE_BOOT;
    if (name == "intro")     return UART_STATE_INTRO;
    /*
     * "idle" is the orchestrator's name for the resting screen -- its default,
     * and where STOP lands (orchestrator/src/orchestrator/core.py,
     * apply_capture_result). The panel retired its own Idle screen when the
     * controls moved to physical buttons; INTRO is now that screen, down to the
     * button legend and "Tap Start to begin". Two wire names, one screen, rather
     * than a sixth state that would render identically.
     */
    if (name == "idle")      return UART_STATE_INTRO;
    if (name == "listening") return UART_STATE_LISTENING;
    if (name == "analyzing") return UART_STATE_ANALYZING;
    if (name == "result")    return UART_STATE_RESULT;
    if (name == "error")     return UART_STATE_ERROR;
    return UART_STATE_INVALID;
}

/** @brief Human-readable name for a state id (logging). */
inline const char *uartStateName(int state)
{
    switch (state) {
        case UART_STATE_BOOT:      return "boot";
        case UART_STATE_INTRO:     return "intro";
        case UART_STATE_LISTENING: return "listening";
        case UART_STATE_ANALYZING: return "analyzing";
        case UART_STATE_RESULT:    return "result";
        case UART_STATE_ERROR:     return "error";
        default:                   return "invalid";
    }
}

/* ------------------------------------------------------------------------
 * Decoding
 * ------------------------------------------------------------------------ */

/**
 * @brief Decode one line into a uart_msg_t.
 * @return true if the type tag was recognised. An unrecognised line still
 *         yields UART_MSG_UNKNOWN so the caller can log it.
 */
inline bool uartDecode(const String &json, uart_msg_t &out)
{
    out = uart_msg_t();
    out.has_seq = uartGetULong(json, "seq", out.seq);

    if (uartIsType(json, "state")) {
        out.type = UART_MSG_STATE;
        String s;
        out.state = uartGetString(json, "s", s) ? uartParseStateName(s)
                                                : UART_STATE_INVALID;
        return true;
    }

    if (uartIsType(json, "result")) {
        out.type = UART_MSG_RESULT;
        uartGetString(json, "id", out.id);
        uartGetString(json, "text", out.text);
        uartGetFloat(json, "conf", out.conf);
        return true;
    }

    if (uartIsType(json, "unclear")) {
        /* Below the confidence threshold, or a gloss with no phrase row. A
         * normal outcome, NOT an error -- the device saying "I don't know" is
         * the correct answer when it does not know, and routing it to the error
         * screen would misreport it. Carries conf and nothing else. */
        out.type = UART_MSG_UNCLEAR;
        uartGetFloat(json, "conf", out.conf);
        return true;
    }

    if (uartIsType(json, "status")) {
        out.type = UART_MSG_STATUS;

        unsigned long bat = 0;
        if (uartGetULong(json, "bat", bat)) {
            out.battery     = (uint8_t)(bat > 100 ? 100 : bat);
            out.has_battery = true;
        }

        out.has_mic = uartGetBool(json, "mic_muted", out.mic_muted);
        out.has_spk = uartGetBool(json, "spk_muted", out.spk_muted);
        return true;
    }

    if (uartIsType(json, "ack")) {
        out.type = UART_MSG_ACK;
        return true;
    }

    if (uartIsType(json, "hello")) {
        out.type = UART_MSG_HELLO;
        return true;
    }

    if (uartIsType(json, "button")) {
        out.type = UART_MSG_BUTTON;
        uartGetString(json, "b", out.button);
        return true;
    }

    if (uartIsType(json, "error")) {
        out.type = UART_MSG_ERROR;
        /* The orchestrator spells this field "text"; uartBuildError() below
         * spells it "msg". Accept both rather than making one side wrong --
         * "text" wins because that is what real traffic carries. */
        if (!uartGetString(json, "text", out.msg)) {
            uartGetString(json, "msg", out.msg);
        }
        return true;
    }

    return false;
}

/* ------------------------------------------------------------------------
 * Builders
 * ------------------------------------------------------------------------ */

/** An ack echoes the seq of the message it answers -- never its own counter. */
inline String uartBuildAck(unsigned long seq)
{
    return String("{\"t\":\"ack\",\"seq\":") + seq + "}";
}

inline String uartBuildHello(int version = WIRE_PROTOCOL_VERSION,
                             const char *fw = FIRMWARE_VERSION)
{
    return String("{\"t\":\"hello\",\"v\":") + version + ",\"fw\":\"" + fw + "\"}";
}

/**
 * @brief Build a button event.
 *
 * `on` is written only for mute, where the orchestrator reads it as
 * `bool(msg.get("on", False))` -- so a bare mute press with no flag registers
 * as an UNMUTE every time. Start and stop carry no flag and must not gain one.
 *
 * @param on Resulting mute state. Ignored unless button is "mute".
 */
inline String uartBuildButton(unsigned long &seq, const char *button, bool on = false)
{
    String out = String("{\"t\":\"button\",\"seq\":") + (seq++) +
                 ",\"b\":\"" + button + "\"";
    if (strcmp(button, "mute") == 0) {
        out += ",\"on\":";
        out += (on ? "true" : "false");
    }
    return out + "}";
}

inline String uartBuildError(unsigned long &seq, const char *msg)
{
    return String("{\"t\":\"error\",\"seq\":") + (seq++) +
           ",\"msg\":\"" + msg + "\"}";
}

/* ------------------------------------------------------------------------
 * Transport
 * ------------------------------------------------------------------------ */

/**
 * Newline-framed line reader over any Stream.
 *
 * Reads byte-at-a-time into a fixed buffer rather than calling
 * readStringUntil(), which blocks for its timeout when a line is still in
 * flight. This one never blocks: it returns false until a complete line has
 * arrived, so it is safe to call from a loop that also has to service LVGL.
 */
class UARTReceiver {
public:
    explicit UARTReceiver(Stream &stream) : _stream(stream) {}

    bool available() { return _stream.available() > 0; }

    /**
     * @brief Non-blocking poll for one complete line.
     * @param out Receives the trimmed line when the return value is true.
     * @return true exactly once per newline-terminated line.
     */
    bool readLine(String &out)
    {
        while (_stream.available()) {
            const char c = (char)_stream.read();

            if (c == '\n') {
                out = _buf;
                _buf = "";
                _overflowed = false;
                out.trim();                 /* drops the CR from println() */
                if (out.length() == 0) continue;
                return true;
            }

            if (c == '\r') continue;

            if (_buf.length() >= UART_BUFFER_SIZE) {
                /* Runaway line: keep discarding until the next newline resyncs
                 * us rather than emitting a truncated object the parser would
                 * silently half-accept. */
                _buf = "";
                _overflowed = true;
                continue;
            }

            if (!_overflowed) _buf += c;
        }
        return false;
    }

    /** @brief Blocking single-line read. Kept for the simple sketch flow. */
    String readMessage()
    {
        String s = _stream.readStringUntil('\n');
        s.trim();
        return s;
    }

    void sendMessage(const String &msg) { _stream.println(msg); }

    /** @brief True if the last line was dropped for exceeding the buffer. */
    bool overflowed() const { return _overflowed; }

private:
    Stream &_stream;
    String  _buf;
    bool    _overflowed = false;
};
