/**
 * @file wire_protocol.h
 * Builders and parsers for the SignAlly UART wire protocol (MCU side).
 *
 * Header-only, no dynamic allocation beyond Arduino String, no dependencies.
 * See docs/WIRE_PROTOCOL.md for the schema and for the known limits of the
 * substring-scan parsing used here.
 */

#pragma once
#include <Arduino.h>

/* ------------------------------------------------------------------------
 * Builders
 *
 * Each takes the caller's sequence counter BY REFERENCE and post-increments it,
 * so a message can never be built without consuming a seq -- that is what keeps
 * the counter and the wire in step. createAckMessage() is the exception: an ack
 * echoes the seq it is answering and consumes none of its own.
 * ------------------------------------------------------------------------ */

inline String createHelloMessage(int version = 1, const char *fw = "0.1.0")
{
    return String("{\"t\":\"hello\",\"v\":") + version +
           ",\"fw\":\"" + fw + "\"}";
}

inline String createStateMessage(unsigned long &seq, const String &state)
{
    return String("{\"t\":\"state\",\"seq\":") + (seq++) +
           ",\"s\":\"" + state + "\"}";
}

inline String createResultMessage(unsigned long &seq, const String &id,
                                  const String &text, float conf)
{
    return String("{\"t\":\"result\",\"seq\":") + (seq++) +
           ",\"id\":\"" + id +
           "\",\"text\":\"" + text +
           "\",\"conf\":" + String(conf, 2) + "}";
}

/**
 * @brief Build a status-bar update: battery level and the two mute flags.
 *
 * The flags are stated as MUTES, not as "active", because that is the polarity
 * the icons actually show -- mic and speaker icons appear only to flag a mute.
 * Naming them the other way round is what made the old JSON demo state read
 * backwards; do not reintroduce an "active" spelling here.
 *
 * All three fields are always sent. The display treats a missing field as
 * "leave it alone", so a future partial-update sender is already supported on
 * the far side without a protocol change.
 */
inline String createStatusMessage(unsigned long &seq, uint8_t battery,
                                  bool mic_muted, bool spk_muted)
{
    return String("{\"t\":\"status\",\"seq\":") + (seq++) +
           ",\"bat\":" + battery +
           ",\"mic_muted\":" + (mic_muted ? "true" : "false") +
           ",\"spk_muted\":" + (spk_muted ? "true" : "false") + "}";
}

/**
 * @brief A fault the panel should show: "Move back", "Recognition offline".
 *
 * The field is `text`, matching what the orchestrator emits (core.py). It is
 * NOT `msg` -- an earlier revision of this harness used that spelling and the
 * panel would have shown an empty error screen for every real fault.
 */
inline String createErrorMessage(unsigned long &seq, const String &text)
{
    return String("{\"t\":\"error\",\"seq\":") + (seq++) +
           ",\"text\":\"" + text + "\"}";
}

/**
 * @brief "I did not understand that" -- below threshold, or a gloss with no
 *        row in phrases.json.
 *
 * Carries a confidence and no text: the wording is the panel's to choose. This
 * is a normal outcome and the panel must not render it as an error.
 */
inline String createUnclearMessage(unsigned long &seq, float conf)
{
    return String("{\"t\":\"unclear\",\"seq\":") + (seq++) +
           ",\"conf\":" + String(conf, 2) + "}";
}

/* An ack echoes the seq of the message it answers. Do NOT pass your own
 * counter here -- see docs/WIRE_PROTOCOL.md section 2. */
inline String createAckMessage(unsigned long seq)
{
    return String("{\"t\":\"ack\",\"seq\":") + seq + "}";
}

/* ------------------------------------------------------------------------
 * Parsers
 * ------------------------------------------------------------------------ */

/** @brief Type tag test: does this line carry "t":"<type>"? */
inline bool wireIsType(const String &json, const char *type)
{
    return json.indexOf(String("\"t\":\"") + type + "\"") >= 0;
}

/**
 * @brief Pull a numeric field out of a flat object.
 * @return true if the key was present and parsed.
 */
inline bool wireGetULong(const String &json, const char *key, unsigned long &out)
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

/**
 * @brief Pull a string field out of a flat object.
 *
 * No escape handling: the value ends at the first '"'. A value containing a
 * quote or backslash comes back truncated -- keep wire strings plain.
 *
 * @return true if the key was present.
 */
inline bool wireGetString(const String &json, const char *key, String &out)
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

/**
 * @brief Pull a JSON boolean out of a flat object.
 *
 * Bare `true`/`false` only. A quoted "true" is not a boolean and is rejected,
 * so a sender that stringifies its flags fails visibly rather than reading as
 * false forever.
 */
inline bool wireGetBool(const String &json, const char *key, bool &out)
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
 * @brief Match an ack and recover the seq it is acknowledging.
 * @param seq Receives the acked sequence number (0 for the hello ack).
 */
inline bool parseJSON_ack(const String &json, unsigned long &seq)
{
    if (!wireIsType(json, "ack")) return false;
    if (!wireGetULong(json, "seq", seq)) seq = 0;
    return true;
}

/**
 * @brief Match a physical-button event from the display.
 * @param button_name Receives "start", "stop" or "mute".
 */
inline bool parseJSON_button(const String &json, String &button_name)
{
    if (!wireIsType(json, "button")) return false;
    return wireGetString(json, "b", button_name);
}

/**
 * @brief Match the display's hello and recover its firmware string.
 */
inline bool parseJSON_hello(const String &json, String &fw)
{
    if (!wireIsType(json, "hello")) return false;
    if (!wireGetString(json, "fw", fw)) fw = "?";
    return true;
}
