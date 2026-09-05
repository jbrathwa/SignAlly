import gc
import json
import logging
import threading
import time
import warnings
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
        self.capture_status = 200  # settable to make /capture answer non-2xx
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
        self.server.capture_calls.append(active)
        if self.server.capture_status != 200:
            self.send_error(self.server.capture_status)
            return
        self.server.capture = active
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
    """An idle signer produces no events for minutes; : ping is all there is.

    The gap between pings must exceed READ_TICK_S (0.5 s), or the read loop
    never actually waits and the test proves nothing about what happens when
    it does — which is how the reconnect storm this file now guards against
    survived a green suite.
    """
    offline = threading.Event()
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=offline.set,
                  on_connect=lambda: None, offline_after=2.5)
    up.start()
    try:
        for _ in range(4):
            fake.lines.append(b": ping\n\n")
            time.sleep(1.0)
        assert not offline.is_set()
    finally:
        up.stop()


def test_the_real_two_second_heartbeat_neither_reconnects_nor_loses_events(fake):
    """The cadence recognition actually runs at, which nothing else exercises.

    A socket read timeout used as a wake-up tick poisons the file object the
    first time it fires — CPython latches SocketIO._timeout_occurred and every
    later read raises OSError("cannot read from timed out object") — so the
    stream tore itself down roughly every half second. Measured against this
    cadence that was 6 connections in 8 s, every event in a gap longer than
    the tick dropped, and an offline watchdog that could never fire because
    each reconnect touched the liveness timer.

    All three failures are one assertion set: one connection across several
    heartbeats, and an event three seconds after the last line still arriving.
    """
    events = []
    offline = threading.Event()
    up = Upstream(fake.url, on_event=events.append, on_offline=offline.set,
                  on_connect=lambda: None, backoff=(0.05, 0.1))
    up.start()
    try:
        assert wait_for(lambda: fake.connections >= 1)
        for _ in range(2):
            time.sleep(2.0)
            fake.lines.append(b": ping\n\n")
        assert fake.connections == 1, "reconnected across a plain heartbeat"

        time.sleep(3.0)  # a signer pausing between signs
        fake.lines.append(b'data: {"e":"armed"}\n\n')
        assert wait_for(lambda: events == [{"e": "armed"}], timeout=3.0)
        assert fake.connections == 1
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


def test_set_capture_closes_the_error_body_on_a_non_2xx_response(fake, caplog):
    """A non-2xx POST /capture raises HTTPError, itself an open response body.

    Left unclosed, its __del__ eventually emits a ResourceWarning during
    garbage collection — nondeterministically, often attributed to whatever
    test happens to be running when the collector gets to it.

    Two things make this detectable deterministically, right here:
    - simplefilter("always") + record=True, rather than "error": promoting
      the warning to an error would raise it *inside* __del__, where Python
      treats it as an unraisable exception and silently swallows it instead
      of propagating it to a normal try/except.
    - caplog.at_level(CRITICAL, ...) on this module's logger: set_capture's
      own `log.error(..., exc)` call would otherwise hand pytest's log
      capture a LogRecord whose args tuple holds a live reference to `exc`,
      keeping the HTTPError alive past our gc.collect() and hiding the leak
      until some later, unrelated test happens to trigger collection.
    """
    fake.capture_status = 500
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=lambda: None,
                  on_connect=lambda: None)
    with caplog.at_level(logging.CRITICAL, logger="orchestrator.upstream"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert up.set_capture(True) is False
            gc.collect()
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == [], [str(w.message) for w in leaks]


def test_a_raising_on_event_with_a_non_dict_payload_does_not_tear_down_the_stream(fake):
    """`data: 42` is valid JSON but not a dict; on_event may still raise on it.

    The failure path must not itself raise (e.g. via event.get on a non-dict)
    and escape into the read loop — that would tear down a healthy stream
    over one bad event. A stable connection count is the assertion that
    matters, not what the callback did with the value.
    """
    def boom(event):
        raise ValueError("boom")

    up = Upstream(fake.url, on_event=boom, on_offline=lambda: None,
                  on_connect=lambda: None, backoff=(0.05, 0.1))
    up.start()
    try:
        assert wait_for(lambda: fake.connections >= 1)
        fake.lines.append(b"data: 42\n\n")
        time.sleep(0.3)
        assert fake.connections == 1
    finally:
        up.stop()


def test_stop_returns_promptly_even_mid_read(fake):
    """Regression guard for the bounded wait in _pump.

    With no data pending, stop() must return within a bound well short of the
    thread.join(timeout=2.0) inside it. The reader waits in select() for
    READ_TICK_S, not in the read itself, so it notices _stopping on the next
    tick; a regression to an unguarded blocking read would leave it unable to
    notice until data arrives, making this take the full join timeout instead.
    """
    up = Upstream(fake.url, on_event=lambda e: None, on_offline=lambda: None,
                  on_connect=lambda: None)
    up.start()
    try:
        assert wait_for(lambda: up.connected)
    finally:
        started = time.monotonic()
        up.stop()
        elapsed = time.monotonic() - started
    assert elapsed < 1.5
