"""Tests for the recognition service's HTTP layer.

The event stream is an interface contract another service is coded against, and
a silent change to it breaks that service invisibly. That is why these exist
despite the repo's usual "no tests for plumbing" rule.
"""

from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from recognition.server import MAX_SUBSCRIBERS, EventHub, make_server


def test_subscriber_receives_published_events():
    hub = EventHub()
    q = hub.subscribe()
    hub.publish({"e": "classifying"})
    assert q.get_nowait() == {"e": "classifying"}


def test_take_state_is_retained_for_late_subscribers():
    """A reconnecting orchestrator must not sit blind until the next gesture."""
    hub = EventHub()
    hub.publish({"e": "armed"})
    q = hub.subscribe()
    assert q.get_nowait() == {"e": "armed"}


def test_tracking_is_retained_independently_of_take_state():
    """The two axes move independently; a reconnect needs both to draw a screen."""
    hub = EventHub()
    hub.publish({"e": "tracking", "status": "ok", "hands": 2, "clipped": False})
    hub.publish({"e": "armed"})
    q = hub.subscribe()
    delivered = [q.get_nowait(), q.get_nowait()]
    assert delivered[0]["e"] == "tracking"  # framing first, then state
    assert delivered[1]["e"] == "armed"
    assert q.empty()


def test_only_the_latest_take_state_is_retained():
    hub = EventHub()
    hub.publish({"e": "armed"})
    hub.publish({"e": "recording", "frames": 3})
    q = hub.subscribe()
    assert q.get_nowait() == {"e": "recording", "frames": 3}
    assert q.empty()


def test_transient_events_are_not_retained():
    """classifying lasts ~30 ms. Retaining it would strand a reconnect in
    'analyzing' forever; recognised is a one-shot answer, not a state."""
    hub = EventHub()
    hub.publish({"e": "classifying"})
    hub.publish({"e": "recognised", "gloss": "hello", "conf": 0.9})
    hub.publish({"e": "unclear", "conf": 0.3})
    hub.publish({"e": "fault", "code": "camera_lost", "msg": "gone"})
    q = hub.subscribe()
    assert q.empty()


def test_a_full_queue_drops_the_oldest_event_and_counts_it():
    """The capture thread must never block on a slow reader."""
    hub = EventHub(maxsize=2)
    q = hub.subscribe()
    for i in range(4):
        hub.publish({"e": "recording", "frames": i})
    assert hub.dropped_events == 2
    assert [q.get_nowait()["frames"], q.get_nowait()["frames"]] == [2, 3]


def test_subscribers_are_capped():
    hub = EventHub()
    held = [hub.subscribe() for _ in range(MAX_SUBSCRIBERS)]
    assert all(q is not None for q in held)
    assert hub.subscribe() is None
    assert hub.subscribers == MAX_SUBSCRIBERS


def test_unsubscribe_frees_a_slot_and_is_idempotent():
    hub = EventHub()
    q = hub.subscribe()
    hub.unsubscribe(q)
    assert hub.subscribers == 0
    hub.unsubscribe(q)  # a handler thread may unwind twice; must not raise
    assert hub.subscribers == 0


def test_clear_take_drops_the_retained_take_event_but_keeps_tracking():
    """section 5: 'While paused only tracking is sent.' Without this, a reconnecting
    orchestrator would be handed a take-state event for a take that no longer
    exists — set_capture(False) already destroyed it."""
    hub = EventHub()
    hub.publish({"e": "tracking", "status": "ok", "hands": 2, "clipped": False})
    hub.publish({"e": "recording", "frames": 9})
    hub.clear_take()
    q = hub.subscribe()
    assert q.get_nowait()["e"] == "tracking"
    assert q.empty()  # no stale recording/armed event replayed


def test_clear_take_is_idempotent_and_does_not_disturb_a_fresh_take():
    hub = EventHub()
    hub.clear_take()  # nothing retained yet; must not raise
    hub.publish({"e": "armed"})
    hub.clear_take()
    hub.clear_take()
    q = hub.subscribe()
    assert q.empty()  # armed really was cleared, not just once


def test_publish_reaches_every_subscriber():
    hub = EventHub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.publish({"e": "armed"})
    assert a.get_nowait() == b.get_nowait() == {"e": "armed"}


def test_queue_is_a_plain_queue():
    """The SSE handler blocks on .get(timeout=...); nothing exotic."""
    assert isinstance(EventHub().subscribe(), queue.Queue)


class FakePipeline:
    def __init__(self):
        self.capture = False
        self.calls = []

    def set_capture(self, active: bool) -> bool:
        self.calls.append(active)
        self.capture = bool(active)
        return self.capture

    def health(self) -> dict:
        return {"ok": True, "capture": self.capture, "fps": 4.2, "classes": 262}


@pytest.fixture
def running_server():
    """A real server on an ephemeral port. SSE cannot be tested without a socket."""
    pipeline = FakePipeline()
    hub = EventHub()
    server = make_server(pipeline, hub, port=0, heartbeat=0.2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, pipeline, hub
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def post_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_health_answers_with_the_pipeline_snapshot(running_server):
    base, _, _ = running_server
    with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())["classes"] == 262


def test_health_reports_the_hubs_subscriber_and_drop_counters(running_server):
    """section 5 documents both in /health, and EventHub's own docstring claims
    dropped_events reaches it — the handler must actually merge them in,
    or section 8's slow-subscriber observability never reaches the wire."""
    base, _, hub = running_server
    hub.subscribe()
    hub.subscribe()
    hub.dropped_events = 7
    with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["subscribers"] == 2
    assert payload["dropped_events"] == 7


def test_capture_toggles_the_pipeline(running_server):
    base, pipeline, _ = running_server
    assert post_json(f"{base}/capture", {"active": True}) == (200, {"capture": True})
    assert post_json(f"{base}/capture", {"active": False}) == (200, {"capture": False})
    assert pipeline.calls == [True, False]


def test_capture_rejects_a_body_without_active(running_server):
    base, _, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post_json(f"{base}/capture", {"enabled": True})
    assert excinfo.value.code == 400


def test_capture_rejects_malformed_json(running_server):
    base, _, _ = running_server
    request = urllib.request.Request(f"{base}/capture", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400


def test_capture_rejects_a_non_numeric_content_length(running_server):
    """A malformed Content-Length must not fall out of do_POST unhandled and
    drop the connection with no response at all."""
    base, _, _ = running_server
    request = urllib.request.Request(f"{base}/capture", data=b'{"active": true}', method="POST")
    request.add_header("Content-Length", "abc")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400


def test_capture_rejects_an_oversized_body_with_413(running_server):
    """A well-formed body over MAX_BODY_BYTES must report its own problem
    (too large), not be truncated and then blamed for malformed JSON."""
    base, _, _ = running_server
    oversized = json.dumps({"active": True, "pad": "x" * 5000}).encode()
    request = urllib.request.Request(f"{base}/capture", data=oversized, method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 413


def test_health_survives_a_pipeline_that_raises(running_server):
    """/health is what a watchdog polls; a raising pipeline must yield a 500,
    not a bare dropped connection."""
    base, pipeline, _ = running_server

    def _boom():
        raise RuntimeError("camera thread is dead")

    pipeline.health = _boom
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/health", timeout=5)
    assert excinfo.value.code == 500


def test_health_reports_which_encoder_is_running(running_server):
    """Answering "what is actually installed on the board" should not need ssh."""
    base, _, _ = running_server
    with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
        payload = json.loads(response.read())

    assert len(payload["encoder_fingerprint"]) == 16
    assert payload["islkit_version"]


def test_an_unknown_path_is_404(running_server):
    base, _, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/events", timeout=5)
    assert excinfo.value.code == 404


def read_sse(stream, count):
    """Collect `count` `data:` payloads, skipping heartbeat comments.

    A stall between connect and the next publish longer than the fixture's
    heartbeat would otherwise surface `: ping` where a caller expects an
    event, which is timing-dependent and flakes under load.
    """
    collected = []
    while len(collected) < count:
        line = stream.readline()
        if not line:
            break
        line = line.decode().rstrip("\n")
        if line.startswith("data: "):
            collected.append(json.loads(line[6:]))
    return collected


def read_comment(stream):
    """Read lines until the next heartbeat comment (`: ping`)."""
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode().rstrip("\n")
        if line.startswith(":"):
            return line


def test_results_streams_published_events(running_server):
    base, _, hub = running_server
    with urllib.request.urlopen(f"{base}/results", timeout=5) as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        hub.publish({"e": "classifying"})
        assert read_sse(stream, 1) == [{"e": "classifying"}]


def test_results_replays_retained_state_on_connect(running_server):
    """A reconnecting orchestrator learns the state immediately."""
    base, _, hub = running_server
    hub.publish({"e": "tracking", "status": "ok", "hands": 2, "clipped": False})
    hub.publish({"e": "armed"})
    with urllib.request.urlopen(f"{base}/results", timeout=5) as stream:
        replayed = read_sse(stream, 2)
    assert [event["e"] for event in replayed] == ["tracking", "armed"]


def test_results_heartbeats_while_idle(running_server):
    """The orchestrator declares us offline after 5 s of silence, and an idle
    signer produces no events for minutes."""
    base, _, _ = running_server
    with urllib.request.urlopen(f"{base}/results", timeout=5) as stream:
        assert read_comment(stream) == ": ping"


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_a_dropped_subscriber_frees_its_slot_and_does_not_disturb_the_others(running_server):
    """The property that matters is that the doomed subscriber's hub slot is
    reclaimed — not merely that publishing into an unread queue is harmless,
    which holds trivially even if the slot leaks forever."""
    base, _, hub = running_server
    survivor = urllib.request.urlopen(f"{base}/results", timeout=5)
    doomed = urllib.request.urlopen(f"{base}/results", timeout=5)
    assert hub.subscribers == 2
    doomed.close()

    assert _wait_until(lambda: hub.subscribers == 1)

    hub.publish({"e": "armed"})
    assert read_sse(survivor, 1) == [{"e": "armed"}]
    survivor.close()


def _reset_connect(host, port):
    """Send a bare `/results` request, then force a hard RST via
    SO_LINGER(1, 0) instead of a clean FIN close."""
    sock = socket.create_connection((host, port), timeout=5)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.sendall(b"GET /results HTTP/1.1\r\nHost: x\r\n\r\n")
    sock.close()


def test_a_reset_subscriber_frees_its_slot(running_server):
    """Regression for the leak in `_results` where `hub.subscribe()` ran
    before the `try` that unsubscribes: a client that resets before the
    response headers finish writing raised `BrokenPipeError` out of
    `end_headers()`, escaping `_results` entirely and leaving the queue
    subscribed forever. Enough resets permanently 503s every future
    /results — an orchestrator that restarts at the wrong moment would
    disable its own event stream for good."""
    base, _, hub = running_server
    host, port = base.removeprefix("http://").split(":")
    port = int(port)

    for _ in range(MAX_SUBSCRIBERS + 2):
        _reset_connect(host, port)

    assert _wait_until(lambda: hub.subscribers == 0)

    with urllib.request.urlopen(f"{base}/results", timeout=5) as stream:
        assert stream.status == 200


def test_too_many_subscribers_is_rejected_not_queued(running_server):
    base, _, _ = running_server
    held = [urllib.request.urlopen(f"{base}/results", timeout=5) for _ in range(MAX_SUBSCRIBERS)]
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}/results", timeout=5)
        assert excinfo.value.code == 503
    finally:
        for stream in held:
            stream.close()


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recognition-events-v1.jsonl"
EVENT_TYPES = {
    "armed",
    "recording",
    "classifying",
    "recognised",
    "unclear",
    "tracking",
    "fault",
}


def test_the_consumer_fixture_covers_every_event_type():
    """The orchestrator is built against this file before this service ever runs."""
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    assert {event["e"] for event in events} == EVENT_TYPES


def test_every_fixture_event_survives_sse_framing():
    for line in FIXTURE.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        framed = f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        assert json.loads(framed.split("data: ", 1)[1].strip()) == event


def test_the_fixture_carries_no_display_protocol_keys():
    """These events must never be confusable with display-protocol messages, and
    nothing in this repo may mention seq, ack, UART, LVGL or the panel."""
    text = FIXTURE.read_text()
    for forbidden in ('"t"', '"seq"', '"ack"'):
        assert forbidden not in text
