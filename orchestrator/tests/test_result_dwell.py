"""The result has to stay on screen long enough to read.

Recognition closes a take and re-arms in the same breath, so `recognised` is
followed by `armed` within milliseconds. Translated naively that is a `result`
immediately followed by `state: listening`, and the panel clears the sentence on
any non-RESULT state — measured on hardware 2026-09-07, the sentence survived
under 100 ms and was never readable.

The hold lives here rather than in the panel because the orchestrator is the only
thing that decides what the display shows, and rather than in `core` because it
needs a clock and `core` is pure.
"""

from pathlib import Path

import pytest

from orchestrator.app import Orchestrator, RESULT_DWELL_S
from orchestrator.audio import Player
from orchestrator.hub import EventHub
from orchestrator.phrases import PhraseTable

REPO = Path(__file__).resolve().parents[2]


class StubUpstream:
    def __init__(self, ok=True):
        self.ok, self.calls, self.connected = ok, [], True
        self.last_line_age_s = 0.1

    def set_capture(self, active):
        self.calls.append(active)
        return self.ok


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def rig(tmp_path):
    hub = EventHub()
    phrases = PhraseTable.load(REPO / "phrases.json")
    player = Player(phrases, tmp_path, command=["fake"], runner=lambda argv: None)
    clock = FakeClock()
    app = Orchestrator(phrases, hub, player, StubUpstream(), clock=clock)
    queue_ = hub.subscribe()
    app.on_uplink({"t": "button", "b": "start"})  # capture on, screen listening
    drain(queue_)
    yield app, hub, queue_, clock
    player.close()


def drain(q):
    """Every message published so far."""
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def screens(messages):
    return [m.get("s") for m in messages if m.get("t") == "state"]


def kinds(messages):
    return [m.get("t") for m in messages]


def close_a_take(app, gloss="hello", conf=0.93):
    """One deliberate gesture: the take closes, then the segmenter re-arms."""
    app.on_recognition_event({"e": "classifying"})
    app.on_recognition_event({"e": "recognised", "gloss": gloss, "conf": conf})
    app.on_recognition_event({"e": "armed"})


def test_result_survives_the_immediate_rearm(rig):
    app, _hub, q, _clock = rig
    close_a_take(app)
    sent = drain(q)

    assert "result" in kinds(sent), "the result itself must still be published"
    assert "listening" not in screens(sent), (
        "the re-arm wiped the result off the panel — this is the hardware bug"
    )


def test_listening_lands_once_the_dwell_expires(rig):
    app, _hub, q, clock = rig
    close_a_take(app)
    drain(q)

    clock.advance(RESULT_DWELL_S + 0.01)
    app.flush_holds()

    assert screens(drain(q)) == ["listening"]


def test_the_hold_does_not_release_early(rig):
    app, _hub, q, clock = rig
    close_a_take(app)
    drain(q)

    clock.advance(RESULT_DWELL_S / 2)
    app.flush_holds()

    assert drain(q) == [], "released the screen before the dwell was up"


def test_a_new_take_interrupts_the_hold(rig):
    """A signer who carries straight on must not wait out the dwell."""
    app, _hub, q, _clock = rig
    close_a_take(app)
    drain(q)

    app.on_recognition_event({"e": "classifying"})

    assert screens(drain(q)) == ["analyzing"]


def test_stop_is_never_held(rig):
    """Pressing stop is explicit intent and has to answer immediately."""
    app, _hub, q, _clock = rig
    close_a_take(app)
    drain(q)

    app.on_uplink({"t": "button", "b": "stop"})

    assert screens(drain(q)) == ["idle"]


def test_only_the_latest_deferred_message_survives(rig):
    """Intermediate screens during the hold were never going to be seen."""
    app, _hub, q, clock = rig
    close_a_take(app)
    drain(q)

    app.on_recognition_event({"e": "tracking", "status": "clipped"})   # error
    app.on_recognition_event({"e": "tracking", "status": "ok"})        # state again
    assert drain(q) == [], "nothing may reach the panel during the hold"

    clock.advance(RESULT_DWELL_S + 0.01)
    app.flush_holds()

    sent = drain(q)
    assert len(sent) == 1, f"expected one catch-up message, got {sent}"
    assert sent[0]["t"] == "state"


def test_a_second_result_replaces_the_first_and_restarts_the_hold(rig):
    app, _hub, q, clock = rig
    close_a_take(app, gloss="hello")
    drain(q)

    clock.advance(RESULT_DWELL_S - 0.5)
    close_a_take(app, gloss="mother")
    sent = drain(q)

    assert [m["t"] for m in sent if m["t"] in ("result", "state")][:1] == ["state"]
    assert any(m.get("id") == "mother" for m in sent if m["t"] == "result")
    assert "listening" not in screens(sent), "the new result must get its own dwell"


def test_tracking_chatter_is_held_but_a_fault_is_not(rig):
    """A stale sentence must never outlive the news that the camera died."""
    app, _hub, q, _clock = rig
    close_a_take(app)
    drain(q)

    app.on_recognition_event({"e": "tracking", "status": "clipped"})
    assert drain(q) == [], "'Move back' is chatter and must wait its turn"

    app.on_recognition_event(
        {"e": "fault", "code": "camera_lost", "msg": "camera 2 stopped returning frames"}
    )
    sent = drain(q)
    assert [m["t"] for m in sent] == ["error"]
    assert sent[0]["text"] == "camera 2 stopped returning frames"


def test_recognition_going_offline_is_not_held(rig):
    app, _hub, q, _clock = rig
    close_a_take(app)
    drain(q)

    app.on_offline()

    sent = drain(q)
    assert [m["t"] for m in sent] == ["error"]
    assert sent[0]["text"] == "Recognition offline"
