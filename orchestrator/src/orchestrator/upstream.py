"""The one upstream: recognition's SSE stream, plus POST /capture.

Two things here are load-bearing and easy to get wrong:

1. The liveness timer resets on *any* received line, `: ping` comments included.
   Recognition heartbeats every 2 s and an idle signer produces no events for
   minutes. A timer counting only `data:` lines flashes "Recognition offline"
   at someone standing still.
2. `on_connect` fires on every connection, not just the first. Recognition's
   capture flag is its own and does not survive its restart, so the caller
   re-asserts capture there. It fires once per genuine outage, so it is also
   the one place a synchronous POST on the reader thread is affordable.
3. Nothing here reads with a socket timeout. The first one to fire poisons the
   file object permanently — see `_pump` — which is how a healthy stream that
   merely went quiet becomes a reconnect storm. `stop()` interrupts the read by
   shutting the socket down instead.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:9978"
OFFLINE_AFTER_S = 5.0
CAPTURE_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 5.0
WATCHDOG_TICK_S = 0.5


def _socket_of(stream):
    """The socket under a urllib HTTP response, or None if it cannot be found.

    urllib exposes no handle on it, and two things need one: the connect
    timeout has to be cleared once the connection is up, and `stop()` has to
    interrupt a blocked read. The layout — `HTTPResponse.fp` is a
    `BufferedReader` over a `socket.SocketIO` — is the same on 3.11 through
    3.14. If it ever moves, both callers degrade rather than break.
    """
    try:
        return stream.fp.raw._sock
    except AttributeError:
        log.warning("cannot reach the socket under the upstream response")
        return None


def _interrupt(target) -> None:
    """Unblock a reader parked in recv().

    shutdown() is what makes the blocked read return; closing the file object
    is not guaranteed to wake a reader already inside recv(), which is why the
    socket is worth reaching for at all.
    """
    if target is None:
        return
    try:
        if isinstance(target, socket.socket):
            target.shutdown(socket.SHUT_RDWR)
        else:
            target.close()
    except OSError:
        pass


class Upstream:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        on_event: Callable[[dict], None] = lambda event: None,
        on_offline: Callable[[], None] = lambda: None,
        on_connect: Callable[[], None] = lambda: None,
        offline_after: float = OFFLINE_AFTER_S,
        backoff: tuple[float, float] = (1.0, 5.0),
    ):
        self._base_url = base_url.rstrip("/")
        self._on_event = on_event
        self._on_offline = on_offline
        self._on_connect = on_connect
        self._offline_after = offline_after
        self._backoff_start, self._backoff_max = backoff
        self._stopping = threading.Event()
        self._connected = False
        self._last_line_at = time.monotonic()
        self._reported_offline = False
        self._threads: list[threading.Thread] = []
        self._live_lock = threading.Lock()
        self._live = None  # the socket the reader is parked on, for stop()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_line_age_s(self) -> float:
        return time.monotonic() - self._last_line_at

    def set_callbacks(self, on_event=None, on_offline=None, on_connect=None) -> None:
        """Wire the mediator in after construction — the two refer to each other."""
        if on_event is not None:
            self._on_event = on_event
        if on_offline is not None:
            self._on_offline = on_offline
        if on_connect is not None:
            self._on_connect = on_connect

    def start(self) -> None:
        for target, name in ((self._read_loop, "upstream"), (self._watchdog, "watchdog")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stopping.set()
        with self._live_lock:
            _interrupt(self._live)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def set_capture(self, active: bool) -> bool:
        """POST /capture. False means recognition did not confirm — never raises."""
        body = json.dumps({"active": bool(active)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/capture", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=CAPTURE_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("capture")) == bool(active)
        except Exception as exc:  # noqa: BLE001 - a dead upstream is expected
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # unclosed error bodies (e.g. a 500) leak a socket
            log.error("POST /capture(%s) failed: %r", active, exc)
            return False

    def _touch(self) -> None:
        self._last_line_at = time.monotonic()
        self._reported_offline = False

    def _watchdog(self) -> None:
        while not self._stopping.wait(WATCHDOG_TICK_S):
            if self.last_line_age_s > self._offline_after and not self._reported_offline:
                self._reported_offline = True
                self._connected = False
                try:
                    self._on_offline()
                except Exception:  # noqa: BLE001
                    log.exception("on_offline callback raised")

    def _read_loop(self) -> None:
        delay = self._backoff_start
        while not self._stopping.is_set():
            try:
                # This timeout is a *connect* bound and `_adopt` clears it the
                # moment the connection is up. It must never survive into the
                # reads: CPython's socket.SocketIO.readinto latches
                # _timeout_occurred on the first read timeout, after which
                # every later read on that file object raises
                # OSError("cannot read from timed out object") for good. Used
                # as a wake-up tick it produced ~40 reconnects a minute against
                # the real 2 s heartbeat, each one re-POSTing /capture and
                # touching the liveness timer so the offline watchdog could
                # never fire.
                with urllib.request.urlopen(
                    f"{self._base_url}/results", timeout=CONNECT_TIMEOUT_S
                ) as stream:
                    try:
                        self._adopt(stream)
                        self._connected = True
                        self._touch()
                        delay = self._backoff_start
                        try:
                            self._on_connect()
                        except Exception:  # noqa: BLE001
                            log.exception("on_connect callback raised")
                        self._pump(stream)
                    finally:
                        with self._live_lock:
                            self._live = None
            except Exception as exc:  # noqa: BLE001 - a dead upstream is expected
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # unclosed error bodies (e.g. a 503 refusal) leak a socket
                log.warning("upstream stream ended (%r); reconnecting in %.1fs", exc, delay)
            self._connected = False
            if self._stopping.wait(delay):
                return
            delay = min(delay * 2, self._backoff_max)

    def _adopt(self, stream) -> None:
        """Take the live socket off its connect timeout and hand it to stop().

        The lock closes the race the earlier bounded-read fix was reaching for:
        either stop() sees this socket and interrupts it, or this sees that
        stop() has already run and interrupts it here. Without that, a stop()
        landing between urlopen and the first read would leave the reader
        blocked on a socket nobody holds a reference to.
        """
        sock = _socket_of(stream)
        with self._live_lock:
            self._live = sock if sock is not None else stream
            if sock is not None:
                sock.settimeout(None)
            if self._stopping.is_set():
                _interrupt(self._live)

    def _pump(self, stream) -> None:
        """Read lines until the server hangs up or stop() interrupts the socket.

        A plain blocking readline() is the whole mechanism, and the absence of
        a read timeout is the point: an idle signer's minutes of silence cost
        nothing, and a `: ping` two seconds late is not a disconnect.

        select() was tried here and is wrong. `HTTPResponse` reads its headers
        through a BufferedReader, which routinely pulls the first body bytes
        into a Python-level buffer in the same recv(); select() reports the
        socket as not readable and those already-delivered lines sit unread.
        """
        while not self._stopping.is_set():
            raw = stream.readline()
            if not raw:
                return  # server closed the connection, or stop() shut it down
            self._handle_line(raw)

    def _handle_line(self, raw: bytes) -> None:
        self._touch()  # any line, comments included
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[len("data:"):].strip())
        except json.JSONDecodeError:
            log.warning("undecodable event line, skipping: %r", line[:120])
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - one bad event must not kill the reader
            # event need not be a dict — `data: 42` and `data: [1,2]` are both
            # valid JSON — so build the log identifier defensively; no shape
            # of event may raise here and escape into the outer read loop.
            gloss = event.get("e") if isinstance(event, dict) else None
            log.exception("on_event callback raised for %r", gloss)
