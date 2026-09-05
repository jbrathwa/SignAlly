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
3. The read loop wakes on `select`, never on a socket timeout. A socket read
   timeout poisons the file object permanently the first time it fires — see
   `_pump` — which is how a healthy stream becomes a reconnect storm.
"""

from __future__ import annotations

import json
import logging
import select
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
READ_TICK_S = 0.5
READ_CHUNK = 65536


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
                # The timeout here is a *connect* bound. It must never be used
                # as a read tick: CPython's socket.SocketIO.readinto latches
                # _timeout_occurred on the first read timeout, after which every
                # further read on that file object raises
                # OSError("cannot read from timed out object") forever. A
                # per-read timeout therefore turns a quiet-but-healthy stream
                # into a reconnect storm — ~40 reconnects a minute against the
                # real 2 s heartbeat, each one re-POSTing /capture and resetting
                # the liveness timer so the offline watchdog can never fire.
                # _pump uses select() as the wake-up mechanism instead.
                with urllib.request.urlopen(
                    f"{self._base_url}/results", timeout=CONNECT_TIMEOUT_S
                ) as stream:
                    self._connected = True
                    self._touch()
                    delay = self._backoff_start
                    try:
                        self._on_connect()
                    except Exception:  # noqa: BLE001
                        log.exception("on_connect callback raised")
                    self._pump(stream)
            except Exception as exc:  # noqa: BLE001 - a dead upstream is expected
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # unclosed error bodies (e.g. a 503 refusal) leak a socket
                log.warning("upstream stream ended (%r); reconnecting in %.1fs", exc, delay)
            self._connected = False
            if self._stopping.wait(delay):
                return
            delay = min(delay * 2, self._backoff_max)

    def _pump(self, stream) -> None:
        """Read lines off a live stream until it ends or stop() is called.

        select() is what wakes this thread every READ_TICK_S to notice
        _stopping, so the socket itself is never asked to time out and an idle
        signer's minutes of silence cost nothing. stop() stays bounded because
        the wait happens in select, not in the read.

        read1() rather than readline() is load-bearing for that: it takes at
        most one recv() and drains the response's buffer, so "select says
        nothing is readable" really does mean nothing is pending. readline()
        can leave a second complete line sitting in the buffered reader where
        select cannot see it, and it would sit there unread until more bytes
        happened to arrive.

        Only reading a socket select has already called readable also means the
        connect timeout inherited from urlopen cannot fire here, so it cannot
        latch either.
        """
        buffer = b""
        while not self._stopping.is_set():
            try:
                ready, _, _ = select.select([stream.fileno()], [], [], READ_TICK_S)
            except (OSError, ValueError):
                return  # the stream was closed underneath us
            if not ready:
                continue
            chunk = stream.read1(READ_CHUNK)
            if not chunk:
                return  # server closed the connection
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._handle_line(line)

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
