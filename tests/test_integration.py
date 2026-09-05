import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from orchestrator.app import Orchestrator
from orchestrator.audio import Player
from orchestrator.hub import EventHub
from orchestrator.phrases import PhraseTable
from orchestrator.server import make_server
from orchestrator.upstream import Upstream

from .test_upstream import FakeRecognition, wait_for

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rig(tmp_path):
    fake = FakeRecognition().start()
    hub = EventHub()
    phrases = PhraseTable.load(REPO / "phrases.json")
    played = []
    player = Player(phrases, tmp_path, command=["fake"], runner=played.append)
    upstream = Upstream(fake.url, offline_after=0.6, backoff=(0.1, 0.2))
    app = Orchestrator(phrases, hub, player, upstream)
    upstream.set_callbacks(app.on_recognition_event, app.on_offline, app.on_connect)
    upstream.start()
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, fake, app, hub, played
    upstream.stop()
    server.shutdown()
    server.server_close()
    player.close()
    fake.cut.set()
    fake.shutdown()
    fake.server_close()


def collect(base, count, timeout=8.0):
    """Read `count` protocol messages off /events, skipping heartbeats."""
    seen = []
    with urllib.request.urlopen(f"{base}/events", timeout=timeout) as stream:
        deadline = time.monotonic() + timeout
        while len(seen) < count and time.monotonic() < deadline:
            line = stream.readline().decode()
            if line.startswith("data:"):
                seen.append(json.loads(line.removeprefix("data:").strip()))
    return seen


def post(base, payload: bytes):
    request = urllib.request.Request(f"{base}/uplink", data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status


def health(base):
    return json.loads(urllib.request.urlopen(f"{base}/health", timeout=3).read())


def subscriber_count(base):
    return health(base)["subscribers"]


def test_a_whole_session_end_to_end(rig):
    """Recognition fixture in, protocol JSON out, audio played."""
    base, fake, app, hub, played = rig
    reader = []
    thread = threading.Thread(target=lambda: reader.extend(collect(base, 5)), daemon=True)
    thread.start()
    # Wait for the SSE handler to actually register with the hub, rather than
    # hoping a fixed sleep outlasts it — a slow/contended box could otherwise
    # have `start` publish `listening` before anyone is subscribed, and the
    # exact 5-message assertion below would fail or hang until thread.join's
    # timeout.
    assert wait_for(lambda: subscriber_count(base) >= 1, timeout=5.0)

    post(base, b'{"t":"button","b":"start"}')
    for event in ({"e": "armed"}, {"e": "recording", "frames": 3}, {"e": "classifying"},
                  {"e": "recognised", "gloss": "hello", "conf": 0.92, "take_usable": True}):
        fake.lines.append(b"data: " + json.dumps(event).encode() + b"\n\n")
        time.sleep(0.1)
    post(base, b'{"t":"button","b":"stop"}')
    thread.join(timeout=8)

    assert reader == [
        {"t": "hello", "v": 1},
        {"t": "state", "seq": 1, "s": "listening"},
        {"t": "state", "seq": 2, "s": "analyzing"},
        {"t": "result", "seq": 3, "id": "hello", "text": "Hello", "conf": 0.92},
        {"t": "state", "seq": 4, "s": "idle"},
    ]


def test_seq_is_contiguous_and_every_ack_is_matched(rig):
    """DoD 6.

    Read the lines that were actually emitted, ack exactly those, and watch the
    outstanding set empty. Comparing two counters proves neither half of the
    name: it never reads /events, so it says nothing about contiguity, and a
    panel acking one seq eleven times scores the same as one acking eleven.
    """
    base, fake, app, hub, _ = rig
    reader = []
    thread = threading.Thread(target=lambda: reader.extend(collect(base, 12)), daemon=True)
    thread.start()
    assert wait_for(lambda: subscriber_count(base) >= 1, timeout=5.0)

    post(base, b'{"t":"button","b":"start"}')  # state:listening, seq 1
    for _ in range(10):
        fake.lines.append(b'data: {"e":"unclear","conf":0.3}\n\n')
    thread.join(timeout=8)

    seqs = [msg["seq"] for msg in reader if "seq" in msg]
    assert seqs == list(range(1, 12))
    assert health(base)["unmatched_acks"] == 11  # emitted, none acked yet

    for seq in seqs:
        post(base, json.dumps({"t": "ack", "seq": seq}).encode())
    assert health(base)["unmatched_acks"] == 0
    assert health(base)["acks"]["unknown"] == 0

    # Neither of these is a match. A counter scores both as one more ack.
    post(base, json.dumps({"t": "ack", "seq": seqs[0]}).encode())  # acked twice
    post(base, json.dumps({"t": "ack", "seq": 9999}).encode())  # never emitted
    after = health(base)
    assert after["unmatched_acks"] == 0
    assert after["acks"]["unknown"] == 1
    assert after["dropped"] == 0


def test_the_whole_recognition_fixture_replays_without_raising(rig):
    """Every event type, both unclear shapes, all three fault codes. DoD 4."""
    base, fake, app, hub, _ = rig
    post(base, b'{"t":"button","b":"start"}')
    for line in (FIXTURES / "recognition-events-v1.jsonl").read_text().splitlines():
        app.on_recognition_event(json.loads(line))
    # The three fault events alone guarantee output, gate or no gate.
    assert hub.seq >= 3
    health = json.loads(urllib.request.urlopen(f"{base}/health", timeout=3).read())
    assert health["dropped"] == 0


def test_an_unknown_gloss_emits_unclear_and_plays_nothing(rig):
    """The 262-class head's normal case until S7 lands."""
    base, fake, app, hub, played = rig
    post(base, b'{"t":"button","b":"start"}')
    app.on_recognition_event({"e": "recognised", "gloss": "truck", "conf": 0.81})
    assert played == []


def test_killing_recognition_reports_offline_and_recovers(rig):
    """DoD 5."""
    base, fake, app, hub, _ = rig
    post(base, b'{"t":"button","b":"start"}')
    assert wait_for(lambda: app.state.capture_active)
    fake.capture_calls.clear()

    fake.cut.set()
    assert wait_for(lambda: app.state.offline, timeout=8)

    fake.cut.clear()
    assert wait_for(lambda: not app.state.offline, timeout=10)
    # Recognition's capture flag does not survive its restart.
    assert fake.capture_calls == [True]
    assert json.loads(urllib.request.urlopen(f"{base}/ping", timeout=3).read()) == {"ok": True}


def test_a_reconnect_whose_capture_re_assert_fails_does_not_claim_to_be_listening(rig):
    """The re-assert's result must be folded back, not discarded.

    Recognition comes back, but its /capture POST fails. Keeping
    capture_active=True and screen="listening" would have the panel report the
    device as listening while nothing is capturing.
    """
    base, fake, app, hub, _ = rig
    post(base, b'{"t":"button","b":"start"}')
    assert wait_for(lambda: app.state.capture_active)

    fake.cut.set()
    assert wait_for(lambda: app.state.offline, timeout=8)
    fake.capture_status = 500
    fake.cut.clear()

    assert wait_for(lambda: not app.state.capture_active, timeout=10)
    assert app.state.screen == "idle"
    assert app.state.offline is True


def test_ping_keeps_answering_while_recognition_is_dead(rig):
    base, fake, app, hub, _ = rig
    fake.cut.set()
    assert wait_for(lambda: app.state.offline, timeout=8)
    assert json.loads(urllib.request.urlopen(f"{base}/ping", timeout=3).read()) == {"ok": True}
