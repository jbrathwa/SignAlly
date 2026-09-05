from dataclasses import replace
from pathlib import Path

from orchestrator.core import (
    SCREEN_LISTENING,
    DeviceState,
    mark_offline,
    mark_online,
    translate,
)
from orchestrator.phrases import PhraseTable

TABLE = PhraseTable.load(Path(__file__).resolve().parents[1] / "phrases.json")
CAPTURING = DeviceState(screen=SCREEN_LISTENING, capture_active=True)


def test_clipped_becomes_move_back():
    out = translate({"e": "tracking", "status": "clipped", "clipped": True}, CAPTURING, TABLE)
    assert out.messages == ({"t": "error", "text": "Move back"},)
    assert out.state.tracking_status == "clipped"


def test_absent_becomes_nobody_in_frame():
    out = translate({"e": "tracking", "status": "absent", "hands": 0}, CAPTURING, TABLE)
    assert out.messages == ({"t": "error", "text": "Nobody in frame"},)


def test_hands_hidden_is_logged_only():
    out = translate({"e": "tracking", "status": "hands_hidden"}, CAPTURING, TABLE)
    assert out.messages == ()
    assert out.state.tracking_status == "hands_hidden"
    assert out.logs


def test_the_same_status_twice_emits_once():
    state = replace(CAPTURING, tracking_status="clipped")
    out = translate({"e": "tracking", "status": "clipped"}, state, TABLE)
    assert out.messages == ()


def test_ok_after_an_error_re_emits_the_current_state():
    """Nothing else clears the panel's error screen — there is no 'error over'."""
    state = DeviceState(screen=SCREEN_LISTENING, capture_active=True, tracking_status="clipped")
    out = translate({"e": "tracking", "status": "ok", "hands": 2}, state, TABLE)
    assert out.messages == ({"t": "state", "s": "listening"},)
    assert out.state.tracking_status == "ok"


def test_ok_after_hands_hidden_emits_nothing():
    """No error was shown, so there is nothing to clear."""
    state = DeviceState(screen=SCREEN_LISTENING, capture_active=True,
                        tracking_status="hands_hidden")
    out = translate({"e": "tracking", "status": "ok"}, state, TABLE)
    assert out.messages == ()


def test_tracking_is_ignored_while_not_capturing():
    """An idle device must not nag an empty room."""
    out = translate({"e": "tracking", "status": "absent"}, DeviceState(), TABLE)
    assert out.messages == ()


def test_mark_offline_emits_once_and_latches():
    first = mark_offline(DeviceState(capture_active=True))
    assert first.messages == ({"t": "error", "text": "Recognition offline"},)
    assert first.state.offline is True
    assert mark_offline(first.state).messages == ()


def test_mark_online_re_emits_state_and_clears_the_latch():
    offline = DeviceState(screen=SCREEN_LISTENING, capture_active=True, offline=True)
    out = mark_online(offline)
    assert out.messages == ({"t": "state", "s": "listening"},)
    assert out.state.offline is False


def test_mark_online_when_never_offline_emits_nothing():
    assert mark_online(DeviceState()).messages == ()
