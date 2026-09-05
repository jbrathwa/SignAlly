"""SignAlly console — a stand-in for the CrowPanel, running inside App Lab.

The physical panel is not attached yet, so this takes its place. It does
everything the panel does that the orchestrator can observe:

  * subscribes to the orchestrator's SSE `/events` and mirrors every protocol
    line into a browser page,
  * acks each `seq` back on `/uplink`, exactly as the panel's firmware does,
  * turns the page's START / STOP / MUTE buttons into real `{"t":"button"}`
    messages,
  * sends a `hello` on connect so the orchestrator replies and resends state.

There is no Bridge and no UART here on purpose. The MCU sketch still owns its
own mock state machine and its Bridge code is disabled, so wiring through it
would mean two components minting `seq`. When the panel arrives, the Bridge hop
slots in underneath this file without changing anything the orchestrator sees.

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
from arduino.app_utils import App, Logger

logger = Logger("SignAllyConsole")
ui = WebUI()

PORT = 9977
LAN_FALLBACK = "192.168.1.19"
PROBE_TIMEOUT_S = 3
POST_TIMEOUT_S = 3
RECONNECT_DELAY_S = 2.0
BACKLOG_MAX = 200

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


def _handle_down(msg: dict):
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

    _record("down", msg)
    _push_status()

    # The panel acks by seq. `hello` carries none, and the firmware answers it
    # with an ack of seq 0 — mirror that so the orchestrator sees the same
    # traffic it will see from real hardware.
    if not auto_ack:
        return
    if kind == "hello":
        post_uplink({"t": "ack", "seq": 0})
    elif seq is not None:
        post_uplink({"t": "ack", "seq": seq})


def pump():
    """Subscribe to /events and forward forever. Reconnects on its own."""
    while True:
        host = find_host()
        with _state_lock:
            _state["host"] = host
            _state["linked"] = False
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
                    _handle_down(msg)

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
    """Let an operator stop acking, to watch what a silent panel looks like."""
    value = bool((data or {}).get("on", True))
    with _state_lock:
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


ui.on_message("button", on_button)
ui.on_message("hello", on_hello)
ui.on_message("set_auto_ack", on_set_auto_ack)
ui.on_connect(on_ui_connect)

threading.Thread(target=pump, name="orchestrator-pump", daemon=True).start()

App.run()
