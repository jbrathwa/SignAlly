import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestrator.upstream import Upstream


class FakeRecognition(ThreadingHTTPServer):
    """Reproduces what actually bites: comment lines, abrupt disconnects."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lines: list[bytes] = []
        self.capture = False
        self.capture_calls: list[bool] = []
        self.cut = threading.Event()
        self.connections = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/results":
            self.send_error(404)
            return
        if self.server.cut.is_set():
            # Refuse outright rather than accepting and hanging up: an accepted
            # connection fires the client's on_connect, and a reconnect loop
            # would then re-assert capture on every single retry.
            self.send_error(503)
            return
        self.server.connections += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        while not self.server.cut.is_set():
            if self.server.lines:
                self.wfile.write(self.server.lines.pop(0))
                self.wfile.flush()
            else:
                time.sleep(0.01)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        active = bool(json.loads(body)["active"])
        self.server.capture = active
        self.server.capture_calls.append(active)
        payload = json.dumps({"capture": active}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake():
    server = FakeRecognition().start()
    yield server
    server.cut.set()
    server.shutdown()
    server.server_close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_data_lines_reach_on_event(fake):
    events = []
    up = Upstream(fake.url, on_event=events.append, on_offline=lambda: None,
                  on_connect=lambda: None)
    up.start()
    try:
        fake.lines.append(b'data: {"e":"armed"}\n\n')
        assert wait_for(lambda: events == [{"e": "armed"}])
    finally:
        up.stop()


def test_a_comment_line_resets_the_liveness_timer(fake):
    """An idle signer produces no events for minutes; : ping is all there is."""
    offline = threading.Event()
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=offline.set,
                  on_connect=lambda: None, offline_after=0.6)
    up.start()
    try:
        for _ in range(6):
            fake.lines.append(b": ping\n\n")
            time.sleep(0.15)
        assert not offline.is_set()
    finally:
        up.stop()


def test_silence_past_the_deadline_reports_offline(fake):
    offline = threading.Event()
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=offline.set,
                  on_connect=lambda: None, offline_after=0.4)
    up.start()
    try:
        assert offline.wait(5)
    finally:
        up.stop()


def test_a_malformed_data_line_is_skipped_not_fatal(fake):
    events = []
    up = Upstream(fake.url, on_event=events.append, on_offline=lambda: None,
                  on_connect=lambda: None)
    up.start()
    try:
        fake.lines.append(b"data: {not json\n\n")
        fake.lines.append(b'data: {"e":"armed"}\n\n')
        assert wait_for(lambda: events == [{"e": "armed"}])
    finally:
        up.stop()


def test_set_capture_posts_and_reports_success(fake):
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=lambda: None,
                  on_connect=lambda: None)
    assert up.set_capture(True) is True
    assert fake.capture_calls == [True]


def test_set_capture_returns_false_when_recognition_is_unreachable():
    up = Upstream("http://127.0.0.1:1", on_event=lambda e: None,
                  on_offline=lambda: None, on_connect=lambda: None)
    assert up.set_capture(True) is False


def test_on_connect_fires_for_every_connection_including_reconnects(fake):
    connects = []
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=lambda: None,
                  on_connect=lambda: connects.append(1), backoff=(0.05, 0.1))
    up.start()
    try:
        assert wait_for(lambda: len(connects) >= 1)
        fake.cut.set()
        time.sleep(0.3)
        fake.cut.clear()
        assert wait_for(lambda: len(connects) >= 2, timeout=8)
    finally:
        up.stop()


def test_stop_is_idempotent(fake):
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=lambda: None,
                  on_connect=lambda: None)
    up.start()
    up.stop()
    up.stop()
