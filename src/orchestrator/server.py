"""The API the App Lab courier talks to. Binds 0.0.0.0 — loopback would break
the display chain, because the container reaches this across the docker gateway.

The courier parses none of this. It forwards whole lines.
"""

from __future__ import annotations

import json
import logging
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .protocol import ProtocolError, encode

log = logging.getLogger(__name__)

DEFAULT_PORT = 9977
MAX_BODY = 4096
HEARTBEAT_S = 2.0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path == "/ping":
            self._json(200, {"ok": True})
        elif self.path == "/health":
            self._json(200, self.app.health())
        elif self.path == "/events":
            self._events()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/uplink":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "bad Content-Length")
            return
        if length < 0:
            self.send_error(400, "bad Content-Length")
            return
        if length > MAX_BODY:
            self.send_error(413, "body too large")
            return
        try:
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "body is not JSON")
            return
        if not isinstance(msg, dict):
            self.send_error(400, "body is not a JSON object")
            return
        # An unknown `t` reaches here and returns 200: rule 1 makes it forward
        # compatibility, not an error.
        self.app.on_uplink(msg)
        self._json(200, {"ok": True})

    def _events(self) -> None:
        subscriber = self.app.hub.subscribe()
        if subscriber is None:
            self.send_error(503, "too many subscribers")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                try:
                    message = subscriber.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                try:
                    line = encode(message)
                except ProtocolError as exc:
                    log.error("dropping unframeable message: %s", exc)
                    continue
                self.wfile.write(b"data: " + line + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.info("events subscriber went away")
        finally:
            self.app.hub.unsubscribe(subscriber)


class OrchestratorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, _Handler)


def make_server(app, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> OrchestratorServer:
    return OrchestratorServer((host, port), app)
