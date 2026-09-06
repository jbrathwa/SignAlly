"""The recognition service's HTTP layer. No camera, no torch, no MediaPipe.

Camera in, glosses out — this half is only the "out". It knows nothing about
what a screen is, how the consuming service numbers its messages, or what
language the device speaks; those belong to the orchestrator. If a change to
the display protocol would require touching this file, the boundary has been
drawn wrong.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_SUBSCRIBERS = 4
QUEUE_MAXSIZE = 256
HEARTBEAT_SECONDS = 2.0

# Which events are worth replaying to a subscriber that connects late, and under
# which slot. Two slots, not one: framing and take state move independently, so a
# reconnecting orchestrator needs both to draw a correct screen. `classifying`
# lasts ~30 ms and `recognised`/`unclear`/`fault` are one-shot answers — retaining
# any of them would strand a reconnect in a state that has long since passed.
_RETAIN_SLOT = {"armed": "take", "recording": "take", "tracking": "tracking"}
_REPLAY_ORDER = ("tracking", "take")  # framing first, then what we are doing


class EventHub:
    """Fan-out from one capture thread to N SSE subscribers.

    `publish` never blocks and never raises. That is the whole point: the capture
    thread is holding the camera and MediaPipe, and a slow or dead orchestrator
    must not be able to stall it. A full queue loses its oldest event and says so
    in `dropped_events`, which `/health` reports.
    """

    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS, maxsize: int = QUEUE_MAXSIZE):
        self._max_subscribers = max_subscribers
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._queues: list[queue.Queue] = []
        self._retained: dict[str, dict] = {}
        self.dropped_events = 0

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._queues)

    def subscribe(self) -> queue.Queue | None:
        """A queue pre-seeded with the retained state, or None if we are full."""
        with self._lock:
            if len(self._queues) >= self._max_subscribers:
                return None
            q: queue.Queue = queue.Queue(maxsize=self._maxsize)
            for slot in _REPLAY_ORDER:
                event = self._retained.get(slot)
                if event is not None:
                    q.put_nowait(event)
            self._queues.append(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Idempotent: a handler thread can unwind through more than one path."""
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def clear_take(self) -> None:
        """Drop the retained take-state event, leaving `tracking` retained.

        Pausing destroys any in-flight take (`RecognitionPipeline.set_capture`
        disarms it), but without this the hub would go on replaying the last
        `armed`/`recording` to every new subscriber — section 5: "While paused only
        `tracking` is sent." Idempotent: popping an already-empty slot is a
        no-op, so it is safe to call on every pause, not just a transition.
        """
        with self._lock:
            self._retained.pop("take", None)

    def publish(self, event: dict) -> None:
        with self._lock:
            slot = _RETAIN_SLOT.get(event.get("e"))
            if slot is not None:
                self._retained[slot] = event
            for q in self._queues:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        q.get_nowait()  # drop the oldest, keep the newest
                    except queue.Empty:  # pragma: no cover - drained concurrently
                        pass
                    self.dropped_events += 1
                    try:
                        q.put_nowait(event)
                    except queue.Full:  # pragma: no cover - drained concurrently
                        pass


DEFAULT_PORT = 9978  # the orchestrator takes 9977
MAX_BODY_BYTES = 4096

log = logging.getLogger("recognition.server")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "islkit-recognition/1"

    # -- helpers -----------------------------------------------------------

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0]
        if path == "/results":
            self._results()
        elif path == "/health":
            try:
                payload = self.server.pipeline.health()
            except Exception:
                # /health is what a watchdog polls; a bare disconnect on our
                # side is the least useful failure it could see.
                log.exception("pipeline.health() failed")
                self._json(500, {"error": "health check failed"})
                return
            # The hub's own counters, not the pipeline's: section 5 documents both in
            # /health, and without this merge they are only ever exercised by
            # the hub's own tests — section 8's slow-subscriber observability never
            # actually reaches the wire.
            payload = {
                **payload,
                "subscribers": self.server.hub.subscribers,
                "dropped_events": self.server.hub.dropped_events,
            }
            self._json(200, payload)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/capture":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body must be under {MAX_BODY_BYTES} bytes"})
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"")
            active = payload["active"]
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": 'body must be {"active": true|false}'})
            return
        if not isinstance(active, bool):
            self._json(400, {"error": '"active" must be true or false'})
            return

        self._json(200, {"capture": self.server.pipeline.set_capture(active)})

    def _results(self) -> None:
        """SSE. No Content-Length: the body is delimited by the close, which is
        what lets this stream indefinitely."""
        q = self.server.hub.subscribe()
        if q is None:
            self._json(503, {"error": "too many subscribers"})
            return

        # Everything from here on can raise if the peer is already gone (a
        # reset between accept and first byte hits end_headers itself) — the
        # try must cover the header writes too, or a vanished client leaks
        # its hub slot forever.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                try:
                    event = q.get(timeout=self.server.heartbeat)
                    payload = f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                except queue.Empty:
                    # A comment, not an event: the orchestrator's 5 s offline
                    # timer must reset on any received line, and an idle signer
                    # produces no events for minutes.
                    payload = ": ping\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the orchestrator went away; that is its right
        finally:
            self.server.hub.unsubscribe(q)


class RecognitionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, pipeline, hub, heartbeat):
        super().__init__(address, _Handler)
        self.pipeline = pipeline
        self.hub = hub
        self.heartbeat = heartbeat


def make_server(
    pipeline,
    hub: EventHub,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> RecognitionServer:
    """Bound to loopback by default. Nothing outside this machine — and nothing
    inside an App Lab container — talks to this service directly."""
    return RecognitionServer((host, port), pipeline, hub, heartbeat)


def serve(pipeline, hub: EventHub, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    server = make_server(pipeline, hub, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
