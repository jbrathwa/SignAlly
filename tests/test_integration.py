import json
import queue
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
    # Real wav files under tmp_path. With an empty audio directory every gloss
    # is skipped as a missing file, `played` is [] no matter what happened, and
    # "plays nothing" stops discriminating between any two glosses.
    for gloss in phrases.glosses():
        wav = tmp_path / phrases.get(gloss).audio
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF")
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


def drain(q):
    """Everything queued for one hub subscriber, without blocking."""
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


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
    assert wait_for(lambda: len(played) == 1)
    assert Path(played[0][-1]).name == "hello.wav"


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


def test_an_ack_of_seq_zero_is_the_hello_ack_not_an_anomaly(rig):
    """Found by running the console against a live orchestrator on the board.

    `hello` carries no seq, and the panel firmware answers it with an ack of
    seq 0 — its own expected_flow.txt documents that convention. Our counter
    starts at 1, so 0 is never one of ours, and scoring it as an anomaly ticked
    the counter on every panel boot, drowning the signal it exists for. The
    firmware is already written; the orchestrator tolerates what the hardware
    actually sends.
    """
    base, fake, app, hub, _ = rig
    post(base, b'{"t":"hello","v":1,"fw":"0.1.0"}')
    post(base, b'{"t":"ack","seq":0}')

    after = health(base)
    assert after["acks"]["received"] == 1
    assert after["acks"]["unknown"] == 0
    # Answering hello resends `state`, which consumes a seq and is genuinely
    # unacked — so one outstanding here is correct, and the seq-0 ack neither
    # matched it nor counted against it.
    assert after["unmatched_acks"] == 1


def test_the_whole_recognition_fixture_replays_to_the_expected_message_sequence(rig):
    """Every event type, both unclear shapes, all three fault codes. DoD 4.

    Spec section 10 asks for the message *sequence*, not for evidence that something
    came out. "at least three messages, nothing dropped" is true of an entire
    class of translation regressions.

    Note seq 4: `clipped` -> `hands_hidden` re-emits state, because the trigger
    is leaving an error status rather than arriving at `ok` (section 4).
    """
    base, fake, app, hub, _ = rig
    subscriber = hub.subscribe()
    try:
        post(base, b'{"t":"button","b":"start"}')  # state:listening, seq 1
        drain(subscriber)
        for line in (FIXTURES / "recognition-events-v1.jsonl").read_text().splitlines():
            app.on_recognition_event(json.loads(line))
        assert drain(subscriber) == [
            {"t": "error", "seq": 2, "text": "Nobody in frame"},
            {"t": "error", "seq": 3, "text": "Move back"},
            {"t": "state", "seq": 4, "s": "listening"},
            {"t": "state", "seq": 5, "s": "analyzing"},
            {"t": "result", "seq": 6, "id": "hello", "text": "Hello", "conf": 0.92},
            {"t": "unclear", "seq": 7, "conf": 0.35},
            {"t": "unclear", "seq": 8, "conf": 0.0},
            {"t": "error", "seq": 9, "text": "camera 2 stopped returning frames"},
            {"t": "error", "seq": 10, "text": "camera 2 opens but returns no frames"},
            {"t": "error", "seq": 11, "text": "classify() raised RuntimeError"},
        ]
    finally:
        hub.unsubscribe(subscriber)
    assert health(base)["dropped"] == 0


def test_an_unknown_gloss_emits_unclear_and_plays_nothing(rig):
    """The 262-class head's normal case until S7 lands.

    Both halves need a gloss that *does* play as the control, or "played
    nothing" says nothing: with no wav files on disk it holds for every gloss
    in the table.
    """
    base, fake, app, hub, played = rig
    subscriber = hub.subscribe()
    try:
        post(base, b'{"t":"button","b":"start"}')
        drain(subscriber)
        app.on_recognition_event({"e": "recognised", "gloss": "truck", "conf": 0.81})
        assert drain(subscriber) == [{"t": "unclear", "seq": 2, "conf": 0.81}]
        assert played == []

        app.on_recognition_event({"e": "recognised", "gloss": "hello", "conf": 0.92})
        assert wait_for(lambda: len(played) == 1)
        assert Path(played[0][-1]).name == "hello.wav"
    finally:
        hub.unsubscribe(subscriber)


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
