from pathlib import Path

from orchestrator.core import (
    SCREEN_ANALYZING,
    SCREEN_IDLE,
    SCREEN_LISTENING,
    DeviceState,
    state_message,
    translate,
)
from orchestrator.phrases import PhraseTable

TABLE = PhraseTable.load(Path(__file__).resolve().parents[1] / "phrases.json")
CAPTURING = DeviceState(screen=SCREEN_LISTENING, capture_active=True)


def test_default_state_is_idle_and_not_capturing():
    s = DeviceState()
    assert s.screen == SCREEN_IDLE
    assert s.capture_active is False
    assert s.muted is False
    assert s.tracking_status == "ok"
    assert s.offline is False


def test_state_message_shape():
    assert state_message(DeviceState()) == {"t": "state", "s": "idle"}


def test_armed_from_idle_screen_while_capturing_emits_listening():
    start = DeviceState(screen=SCREEN_IDLE, capture_active=True)
    out = translate({"e": "armed"}, start, TABLE)
    assert out.state.screen == SCREEN_LISTENING
    assert out.messages == ({"t": "state", "s": "listening"},)


def test_recording_when_already_listening_emits_nothing():
    """recording arrives once per frame; un-deduped this floods the UART."""
    out = translate({"e": "recording", "frames": 14}, CAPTURING, TABLE)
    assert out.messages == ()
    assert out.state == CAPTURING


def test_thirty_recording_events_produce_at_most_one_message():
    state, emitted = DeviceState(capture_active=True), []
    for n in range(30):
        out = translate({"e": "recording", "frames": n}, state, TABLE)
        state, emitted = out.state, emitted + list(out.messages)
    assert len(emitted) == 1


def test_classifying_moves_to_analyzing():
    out = translate({"e": "classifying"}, CAPTURING, TABLE)
    assert out.state.screen == SCREEN_ANALYZING
    assert out.messages == ({"t": "state", "s": "analyzing"},)


def test_armed_after_analyzing_returns_to_listening():
    """Returning to listening is observed from recognition, never synthesised."""
    analyzing = DeviceState(screen=SCREEN_ANALYZING, capture_active=True)
    out = translate({"e": "armed"}, analyzing, TABLE)
    assert out.messages == ({"t": "state", "s": "listening"},)


def test_lifecycle_events_are_ignored_while_not_capturing():
    """The idle gate: a take completing after stop must not reach the screen."""
    idle = DeviceState()
    for event in ({"e": "armed"}, {"e": "recording", "frames": 3}, {"e": "classifying"}):
        out = translate(event, idle, TABLE)
        assert out.messages == ()
        assert out.state == idle


def test_unknown_event_type_is_logged_and_ignored():
    out = translate({"e": "somethingnew", "x": 1}, CAPTURING, TABLE)
    assert out.messages == ()
    assert out.state == CAPTURING
    assert out.logs


def test_translate_never_mutates_the_state_it_is_given():
    before = DeviceState(capture_active=True)
    translate({"e": "classifying"}, before, TABLE)
    assert before.screen == SCREEN_IDLE
