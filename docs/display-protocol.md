# Display protocol v1

The line protocol between the orchestrator on the board's Linux side and the ESP32 panel. One JSON
object per line, in both directions.

Speaker: [the orchestrator](orchestrator.md). Renderer: [the panel](panel.md), which documents what
each message draws. Fixtures:
[`display-protocol-v1-down.jsonl`](../orchestrator/tests/fixtures/display-protocol-v1-down.jsonl) ·
[`display-protocol-v1-up.jsonl`](../orchestrator/tests/fixtures/display-protocol-v1-up.jsonl).

---

## 1. The channel it rides on

```
orchestrator ──SSE /events──▶ signally-console ──Bridge──▶ STM32 relay ──UART──▶ panel
     ▲                          (App Lab container)                                  │
     └───────── POST /uplink ◀──────────── Bridge ◀──────────── UART ◀───────────────┘
```

**Nothing between the two ends parses the JSON.** The console and the STM32 relay forward whole
lines as opaque strings. That keeps every decision on the Linux side, and it keeps the STM32,
which has no readable serial output, free of logic that could fail invisibly.

## 2. Framing

| Rule | Enforced where |
|---|---|
| One JSON object per line, `\n`-terminated, UTF-8, no embedded newlines | `protocol.encode` writes compact JSON with no whitespace |
| **256 bytes max per line**, newline included | `protocol.encode` refuses a longer line; the relay's `UART_LINE_MAX` and the panel's `UART_BUFFER_SIZE` are both 256 |
| `text` capped at **120 characters** | `protocol.cap_text` truncates before encoding |
| UART at **115200 8N1** | panel `DISPLAY_UART_BAUD`, relay `MCU_UART_BAUD` |

The limit is not advisory. A line over the buffer is dropped by the panel rather than truncated
into broken JSON, and nothing on the wire can report that, so the orchestrator fails loudly
before sending instead. Non-ASCII text is sent as UTF-8, not `\u` escapes, to save bytes.

## 3. Linux → panel

```json
{"t":"hello","v":1}
{"t":"state","seq":1,"s":"listening"}
{"t":"state","seq":2,"s":"analyzing"}
{"t":"result","seq":3,"id":"hello","text":"Hello","conf":0.92}
{"t":"unclear","seq":4,"conf":0.35}
{"t":"state","seq":5,"s":"idle"}
{"t":"error","seq":6,"text":"Move back"}
```

| `t` | Fields | Means |
|---|---|---|
| `hello` | `v` | The orchestrator is there. Sent first on every `/events` connection and in reply to the panel's `hello`. Carries no `seq` |
| `state` | `seq`, `s` | Show this state. `s` is one of `boot`, `intro`, `listening`, `analyzing`, `idle` |
| `result` | `seq`, `id`, `text`, `conf` | A sign was recognised: show it |
| `unclear` | `seq`, `conf` | A sign was seen but not confidently identified |
| `error` | `seq`, `text` | Something needs the signer's attention: show the text |

`result`, `unclear` and `error` are message types rather than states because they carry a payload.

The orchestrator only ever sends `listening`, `analyzing` and `idle`. `boot` and `intro` belong to
the panel, which runs its own boot splash and treats `idle` and `intro` as the same screen.

### Why `unclear` is not an `error`

When the classifier's top answer is below the confidence threshold, recognition declines to
guess. **This is a normal, frequent outcome.** A protocol that could only say *recognised* or
*broken* would have to encode it as one of those, and both would be false: nothing failed, and
nothing was recognised. A device that admits uncertainty is more trustworthy than one that puts
words in someone's mouth.

`unclear` carries `conf` so logs show how close it came, and no text: the panel's wording
("Didn't catch that. / Please sign again.") never varies, so it lives in the firmware.

### Clearing an error

There is no "error over" message. The orchestrator takes the panel off the error screen by
re-sending a `state` message, for example when tracking recovers.

## 4. Panel → Linux

```json
{"t":"hello","v":1,"fw":"0.1.0"}
{"t":"ack","seq":3}
{"t":"button","seq":7,"b":"start"}
{"t":"button","seq":8,"b":"stop"}
{"t":"button","seq":9,"b":"mute","on":true}
```

| `t` | Fields | Means |
|---|---|---|
| `hello` | `v`, `fw` | The panel booted. The orchestrator replies with `hello` and re-sends the current state |
| `ack` | `seq` | The panel received the line with this `seq`. `seq 0` is the reply to a `hello` |
| `button` | `b`, `on`, `seq` | A physical button was pressed. `b` is `start`, `stop` or `mute`; `on` appears on `mute` only and carries the **resulting** state |

A `button`'s `seq` is the panel's own counter, independent of the orchestrator's, and is ignored
upstream.

## 5. A whole session

```
panel → linux   {"t":"hello","v":1,"fw":"0.1.0"}
linux → panel   {"t":"hello","v":1}
linux → panel   {"t":"state","seq":1,"s":"idle"}
panel → linux   {"t":"ack","seq":1}

panel → linux   {"t":"button","seq":1,"b":"start"}
linux → panel   {"t":"state","seq":2,"s":"listening"}
panel → linux   {"t":"ack","seq":2}

linux → panel   {"t":"state","seq":3,"s":"analyzing"}
panel → linux   {"t":"ack","seq":3}

linux → panel   {"t":"result","seq":4,"id":"hello","text":"Hello","conf":0.92}
panel → linux   {"t":"ack","seq":4}

panel → linux   {"t":"button","seq":2,"b":"stop"}
linux → panel   {"t":"state","seq":5,"s":"idle"}
panel → linux   {"t":"ack","seq":5}
```

## 6. Rules that let it grow

1. **An unknown `t` is logged and ignored. Unknown fields are ignored.** Either side can add a
   message type without reflashing or redeploying the other. The orchestrator answers an unknown
   uplink with `200`. The panel logs an unknown type or state name, drops it and **does not ack
   it**, so a missing ack is a real signal rather than noise.
2. **`id` is the stable key; `text` is a convenience.** The panel's graphics library cannot shape
   Devanagari. When Hindi output lands, the panel can draw a pre-rendered image for `id` and ignore
   `text`: a panel-side change with no schema change.
3. **`seq` is the orchestrator's counter, echoed back in `ack`.** The ack is the only way to observe
   what actually crossed the wire. It is diagnostic, never flow control: a missing ack stalls
   nothing. See [acks in the orchestrator guide](orchestrator.md#7-seq-and-acks).

## 7. Deliberately not in v1

- **`status`** (`bat`, `mic_muted`, `spk_muted`). The panel parses it and draws the icons, but the
  orchestrator never sends it: the panel owns its own battery reading, and mute is decided on the
  panel's side of the wire.
- `tts_playing`, `lang`, and any image or screen-id field. Each can be added under rule 1.
- **`conf` has no renderer.** It is carried for logs during classifier tuning.

## 8. Testing without hardware

- **Orchestrator side:** the fixtures are the test corpus. Every down line must round-trip through
  `encode` byte for byte, and every up line must be accepted by `core` and over HTTP. The last two
  down lines, an unknown `t` and a `result` with an unknown field, exist to catch a parser that
  rejects what rule 1 says to ignore.
- **Panel side:** flash the bench-harness sketch
  ([`mcu/uno_q_mcu_uart_test/`](../mcu/uno_q_mcu_uart_test/)) to the STM32 and type commands
  (`start`, `stop`, `unclear`, `error`, …) into its Serial Monitor. Each one sends real protocol
  lines over the UART, so the panel's whole rendering path can be exercised with nothing running
  on the Linux side. Every `seq` sent must come back as an ack with the same number.
  [`panel.md`](panel.md#the-test) lists the commands.
