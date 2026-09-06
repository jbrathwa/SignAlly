"""SignAlly console — a stand-in for the CrowPanel, running inside App Lab.

The physical panel is not attached yet, so this takes its place. It does
everything the panel does that the orchestrator can observe:

  * subscribes to the orchestrator's SSE `/events` and mirrors every protocol
    line into a browser page,
  * acks each `seq` back on `/uplink`, exactly as the panel's firmware does,
  * turns the page's START / STOP / MUTE buttons into real `{"t":"button"}`
    messages,
  * sends a `hello` on connect so the orchestrator replies and resends state.

When the physical panel is attached it also forwards every line down to it,
over the Bridge to mcu/signally_panel_relay on the STM32, which writes it to the
UART. The panel's own acks and button presses come back the same way and are
POSTed to /uplink unchanged.

The panel is optional and detected, not configured: on each (re)connect this
probes for a relay with `panel_hello`. With one present the console STOPS acking
on the panel's behalf -- the real acks are coming up the wire and a second set
from here would be indistinguishable from a panel that is answering when it is
not. With no relay it behaves exactly as it always did, as a stand-in.

Nothing here parses or rewrites the protocol. Lines go down byte-for-byte as the
orchestrator framed them, which is what keeps its 256-byte guarantee meaningful,
and come back up as the panel wrote them.

The orchestrator is on the Linux host, not in this container, so the host
address is discovered at runtime from /proc/net/route. Do not hard-code the
gateway: it differs per App Lab app, and `host.docker.internal` does not
resolve on this board.
"""

import json
import queue
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge, Logger

logger = Logger("SignAllyConsole")
ui = WebUI()

PORT = 9977
LAN_FALLBACK = "192.168.1.19"
PROBE_TIMEOUT_S = 3
POST_TIMEOUT_S = 3
RECONNECT_DELAY_S = 2.0
BACKLOG_MAX = 200

# Bridge method names. Must match mcu/signally_panel_relay/config.h exactly --
# a typo gives you a console that runs, logs nothing unusual, and never forwards.
RPC_DISPLAY_LINE = "display_line"
RPC_PANEL_UPLINK = "panel_uplink"
RPC_PANEL_HELLO = "panel_hello"

# Short: this runs on every reconnect and a board with no relay flashed must not
# stall the stream for the default 10s to find that out.
PANEL_PROBE_TIMEOUT_S = 2

# Uplink from the panel is handed to a worker rather than POSTed inside the RPC
# callback. A dead orchestrator makes each POST wait for its 3s timeout, and
# doing that in the callback would stall the Bridge's read loop and back up the
# panel's acks behind it.
UPLINK_QUEUE_MAX = 100

# Everything the page needs to redraw itself after a refresh. The browser can
# connect long after the stream started, and a console that only shows what
# arrived while you were watching is not much of a console.
_backlog: "queue.deque[dict]" = __import__("collections").deque(maxlen=BACKLOG_MAX)
_backlog_lock = threading.Lock()

_state = {
    "host": None,        # orchestrator address once found
    "linked": False,     # SSE stream currently open
    "auto_ack": True,    # ack every seq, like the real panel
    "screen": "boot",    # what the panel would be showing
    "last_seq": None,
    "panel": None,       # relay firmware string once probed, else None
}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Finding the orchestrator
# ---------------------------------------------------------------------------

def default_gateway():
    """The container's default route — where the Linux host lives."""
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except Exception as exc:
        logger.warning(f"gateway lookup failed: {exc!r}")
    return None


def find_host():
    """First address whose /ping answers, or None."""
    candidates = [c for c in (default_gateway(), "172.17.0.1", LAN_FALLBACK) if c]
    for candidate in candidates:
        try:
            with urllib.request.urlopen(
                f"http://{candidate}:{PORT}/ping", timeout=PROBE_TIMEOUT_S
            ):
                logger.info(f"orchestrator found at {candidate}:{PORT}")
                return candidate
        except Exception:
            continue
    logger.warning(f"no orchestrator on any of {candidates}")
    return None


# ---------------------------------------------------------------------------
# Talking to the page
# ---------------------------------------------------------------------------

def _record(direction: str, msg: dict, note: str = ""):
    """Push one line to the page and keep it for a later reload."""
    entry = {"dir": direction, "msg": msg, "note": note, "t": time.time()}
    with _backlog_lock:
        _backlog.append(entry)
    try:
        ui.send_message("line", entry)
    except Exception as exc:
        logger.warning(f"could not push line to UI: {exc!r}")


def _push_status():
    with _state_lock:
        snapshot = dict(_state)
    try:
        ui.send_message("status", snapshot)
    except Exception as exc:
        logger.warning(f"could not push status to UI: {exc!r}")


# ---------------------------------------------------------------------------
# Talking to the panel, over the Bridge
# ---------------------------------------------------------------------------

_uplink_q: "queue.Queue[str]" = queue.Queue(maxsize=UPLINK_QUEUE_MAX)


def panel_probe():
    """Ask the STM32 whether a relay is flashed. Returns its fw string or None.

    Every failure mode here means the same thing operationally -- no panel -- so
    they collapse into one None. Distinguishing "no router socket" from "no
    sketch" from "wrong sketch" is a job for the Monitor log, not for this.
    """
    try:
        fw = Bridge.call(RPC_PANEL_HELLO, timeout=PANEL_PROBE_TIMEOUT_S)
        logger.info(f"panel relay present: {fw}")
        return str(fw)
    except Exception as exc:
        logger.info(f"no panel relay ({exc.__class__.__name__}); console stands in")
        return None


def panel_send(raw: str) -> bool:
    """Forward one protocol line to the panel, exactly as it arrived.

    `raw` is the SSE payload, not a re-serialisation of the parsed object: the
    orchestrator already framed it and checked it against its 256-byte limit,
    and re-encoding here would risk changing the byte count it guaranteed.
    """
    try:
        Bridge.notify(RPC_DISPLAY_LINE, raw)
        return True
    except Exception as exc:
        logger.warning(f"panel notify failed: {exc!r}")
        return False


def on_panel_uplink(line):
    """The panel sent something: an ack, a button press, or its hello.

    Runs in the Bridge's callback thread. It only enqueues -- see
    UPLINK_QUEUE_MAX for why the POST does not happen here.
    """
    text = (line or "").strip() if isinstance(line, str) else str(line).strip()
    if not text:
        return
    try:
        _uplink_q.put_nowait(text)
    except queue.Full:
        # Dropping the oldest would reorder acks; dropping the newest at least
        # keeps what we have contiguous. Either way it is a defect signal: the
        # panel emits about one line per second.
        logger.warning("uplink queue full, line dropped")


def uplink_worker():
    """Drain the panel's uplink into POST /uplink, forever."""
    while True:
        text = _uplink_q.get()
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"undecodable uplink from panel, skipped: {text[:120]!r}")
            _record("up", {"raw": text}, note="undecodable")
            continue
        post_uplink(msg)


# ---------------------------------------------------------------------------
# Talking to the orchestrator
# ---------------------------------------------------------------------------

def post_uplink(msg: dict) -> bool:
    """POST one protocol object to /uplink. Never raises."""
    with _state_lock:
        host = _state["host"]
    if not host:
        _record("up", msg, note="not sent — no orchestrator")
        return False

    body = json.dumps(msg).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{PORT}/uplink",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            ok = 200 <= response.status < 300
        _record("up", msg, note="" if ok else f"HTTP {response.status}")
        return ok
    except Exception as exc:
        logger.warning(f"uplink POST failed: {exc!r}")
        _record("up", msg, note=f"failed: {exc.__class__.__name__}")
        return False


def _handle_down(msg: dict, raw: str = ""):
    """One protocol object arrived from the orchestrator."""
    kind = msg.get("t")
    seq = msg.get("seq")

    with _state_lock:
        if kind == "state" and isinstance(msg.get("s"), str):
            _state["screen"] = msg["s"]
        elif kind == "result":
            _state["screen"] = "result"
        elif kind == "unclear":
            _state["screen"] = "unclear"
        elif kind == "error":
            _state["screen"] = "error"
        if seq is not None:
            _state["last_seq"] = seq
        auto_ack = _state["auto_ack"]
        panel = _state["panel"]

    _record("down", msg)
    _push_status()

    # Down to the real panel, if one is attached. Its ack comes back through
    # on_panel_uplink() a few milliseconds later.
    if panel and raw:
        panel_send(raw)

    # The panel acks by seq. `hello` carries none, and the firmware answers it
    # with an ack of seq 0 — mirror that so the orchestrator sees the same
    # traffic it will see from real hardware.
    #
    # Suppressed once a real panel is answering: two acks for one seq would tell
    # the orchestrator its message landed twice, and would keep saying so even
    # after the panel stopped replying. `auto_ack` is forced False when the
    # probe finds a relay, so this is belt and braces.
    if not auto_ack or panel:
        return
    if kind == "hello":
        post_uplink({"t": "ack", "seq": 0})
    elif seq is not None:
        post_uplink({"t": "ack", "seq": seq})


def pump():
    """Subscribe to /events and forward forever. Reconnects on its own."""
    while True:
        host = find_host()

        # Re-probed per connect rather than once at startup: the STM32 can be
        # reflashed, unplugged or reset while this process keeps running, and a
        # console that decided "no panel" at boot would never notice one arrive.
        panel = panel_probe()

        with _state_lock:
            _state["host"] = host
            _state["linked"] = False
            _state["panel"] = panel
            if panel:
                _state["auto_ack"] = False
        _push_status()

        if not host:
            time.sleep(RECONNECT_DELAY_S)
            continue

        try:
            with urllib.request.urlopen(
                f"http://{host}:{PORT}/events", timeout=None
            ) as stream:
                with _state_lock:
                    _state["linked"] = True
                _push_status()
                logger.info("subscribed to /events")

                # Announce ourselves the way the panel firmware does, so the
                # orchestrator replies and resends the current state.
                post_uplink({"t": "hello", "v": 1, "fw": "console-0.1.0"})

                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning(f"undecodable line, skipped: {payload[:120]!r}")
                        continue
                    _handle_down(msg, payload)

        except Exception as exc:
            logger.warning(f"stream ended ({exc!r}); reconnecting")

        with _state_lock:
            _state["linked"] = False
        _push_status()
        time.sleep(RECONNECT_DELAY_S)


# ---------------------------------------------------------------------------
# The page's controls
# ---------------------------------------------------------------------------

def on_button(client, data):
    """START / STOP / MUTE pressed in the page."""
    name = (data or {}).get("b")
    if name not in ("start", "stop", "mute"):
        logger.warning(f"ignoring unknown button {name!r}")
        return
    msg = {"t": "button", "b": name}
    if name == "mute":
        msg["on"] = bool((data or {}).get("on", False))
    post_uplink(msg)


def on_hello(client, data):
    """Pretend the panel just rebooted."""
    post_uplink({"t": "hello", "v": 1, "fw": "console-0.1.0"})


def on_set_auto_ack(client, data):
    """Let an operator stop acking, to watch what a silent panel looks like.

    Turning it back ON is refused while a real panel is attached: the panel is
    already acking, and a second set would misreport every message as having
    landed twice. Turning it off is always allowed.
    """
    value = bool((data or {}).get("on", True))
    with _state_lock:
        if value and _state["panel"]:
            logger.info("auto-ack stays off: a real panel is acking")
            value = False
        _state["auto_ack"] = value
    logger.info(f"auto-ack {'on' if value else 'off'}")
    _push_status()


def on_ui_connect(connection):
    """Replay what the console already saw, then the current status."""
    with _backlog_lock:
        history = list(_backlog)
    try:
        ui.send_message("history", {"lines": history})
    except Exception as exc:
        logger.warning(f"could not replay history: {exc!r}")
    _push_status()


# The panel calls this; register before the pump so a relay that is already
# mid-conversation is never answered with "no such method".
try:
    Bridge.provide(RPC_PANEL_UPLINK, on_panel_uplink)
except Exception as exc:
    # No router socket: running outside App Lab, or the Bridge is unavailable.
    # The console still works as a stand-in, which is the pre-panel behaviour.
    logger.warning(f"could not provide {RPC_PANEL_UPLINK}: {exc!r}")

threading.Thread(target=uplink_worker, name="panel-uplink", daemon=True).start()

ui.on_message("button", on_button)
ui.on_message("hello", on_hello)
ui.on_message("set_auto_ack", on_set_auto_ack)
ui.on_connect(on_ui_connect)

threading.Thread(target=pump, name="orchestrator-pump", daemon=True).start()

App.run()
