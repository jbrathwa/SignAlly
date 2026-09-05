import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from orchestrator.app import Orchestrator
from orchestrator.audio import Player
from orchestrator.hub import EventHub
from orchestrator.phrases import PhraseTable
from orchestrator.server import make_server

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


class StubUpstream:
    def __init__(self, ok=True):
        self.ok, self.calls, self.connected = ok, [], True
        self.last_line_age_s = 0.1

    def set_capture(self, active):
        self.calls.append(active)
        return self.ok


@pytest.fixture
def running(tmp_path):
    hub = EventHub()
    phrases = PhraseTable.load(REPO / "phrases.json")
    player = Player(phrases, tmp_path, command=["fake"], runner=lambda argv: None)
    upstream = StubUpstream()
    app = Orchestrator(phrases, hub, player, upstream)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, app, upstream, hub
    server.shutdown()
    server.server_close()
    player.close()


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def post(url, payload: bytes):
    request = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status


def test_ping_answers_ok(running):
    base, *_ = running
    assert get_json(f"{base}/ping") == {"ok": True}


def test_health_reports_the_whole_picture(running):
    base, *_ = running
    health = get_json(f"{base}/health")
    for key in ("ok", "recognition_connected", "last_line_age_s", "screen", "capture",
                "muted", "tracking", "seq", "subscribers", "dropped", "acks",
                "unmatched_acks", "phrases", "audio", "uptime_s"):
        assert key in health, key
    assert health["phrases"] == 17


def test_unknown_route_is_404(running):
    base, *_ = running
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get_json(f"{base}/nope")
    assert excinfo.value.code == 404
    excinfo.value.close()


def test_start_button_posts_capture_and_emits_listening(running):
    base, app, upstream, _ = running
    assert post(f"{base}/uplink", b'{"t":"button","b":"start"}') == 200
    assert upstream.calls == [True]
    assert app.state.screen == "listening"


def test_stop_button_posts_capture_and_emits_idle(running):
    base, app, upstream, _ = running
    post(f"{base}/uplink", b'{"t":"button","b":"start"}')
    post(f"{base}/uplink", b'{"t":"button","b":"stop"}')
    assert upstream.calls == [True, False]
    assert app.state.screen == "idle"


def test_mute_button_sets_the_gate(running):
    base, app, *_ = running
    post(f"{base}/uplink", b'{"t":"button","b":"mute","on":true}')
    assert app.state.muted is True


def test_every_up_fixture_line_is_accepted_over_http(running):
    """DoD 3, first half."""
    base, *_ = running
    for line in (FIXTURES / "display-protocol-v1-up.jsonl").read_text().splitlines():
        assert post(f"{base}/uplink", line.encode()) == 200


def test_an_unknown_uplink_type_returns_200(running):
    """Rule 1: forward compatibility, not an error."""
    base, *_ = running
    assert post(f"{base}/uplink", b'{"t":"nosuchtype","whatever":true}') == 200


def test_a_body_that_is_not_json_returns_400(running):
    base, *_ = running
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(f"{base}/uplink", b"{not json")
    assert excinfo.value.code == 400
    excinfo.value.close()


def test_an_oversized_body_returns_413(running):
    base, *_ = running
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(f"{base}/uplink", b'{"t":"ack","seq":1,"pad":"' + b"x" * 5000 + b'"}')
    assert excinfo.value.code == 413
    excinfo.value.close()


def test_events_streams_hello_first(running):
    base, app, _, _ = running
    with urllib.request.urlopen(f"{base}/events", timeout=3) as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        first = stream.readline()
        assert json.loads(first.decode().removeprefix("data:").strip()) == {"t": "hello", "v": 1}


def test_events_carries_messages_produced_by_recognition(running):
    base, app, _, _ = running
    with urllib.request.urlopen(f"{base}/events", timeout=5) as stream:
        stream.readline(); stream.readline()          # hello + blank
        app.on_uplink({"t": "button", "b": "start"})
        app.on_recognition_event({"e": "classifying"})
        seen = []
        while len(seen) < 2:
            line = stream.readline().decode()
            if line.startswith("data:"):
                seen.append(json.loads(line.removeprefix("data:").strip()))
    assert seen == [
        {"t": "state", "seq": 1, "s": "listening"},
        {"t": "state", "seq": 2, "s": "analyzing"},
    ]
