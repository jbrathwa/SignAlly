import json
from pathlib import Path

from orchestrator.core import (
    SCREEN_ANALYZING,
    SCREEN_IDLE,
    SCREEN_LISTENING,
    DeviceState,
    apply_capture_result,
    handle_uplink,
    mark_offline,
    mark_online,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_start_requests_capture_but_does_not_change_state_yet():
    out = handle_uplink({"t": "button", "b": "start"}, DeviceState())
    assert out.capture_request is True
    assert out.messages == ()
    assert out.state.screen == SCREEN_IDLE


def test_stop_requests_capture_off():
    out = handle_uplink({"t": "button", "b": "stop"}, DeviceState(capture_active=True))
    assert out.capture_request is False


def test_mute_sets_the_gate_and_emits_nothing():
    """Nothing on screen changes when the speaker is muted."""
    out = handle_uplink({"t": "button", "b": "mute", "on": True}, DeviceState())
    assert out.state.muted is True
    assert out.messages == ()
    assert out.capture_request is None


def test_unmute_clears_the_gate():
    out = handle_uplink({"t": "button", "b": "mute", "on": False}, DeviceState(muted=True))
    assert out.state.muted is False


def test_hello_replies_and_resends_the_current_state():
    state = DeviceState(screen=SCREEN_LISTENING, capture_active=True)
    out = handle_uplink({"t": "hello", "v": 1, "fw": "0.1.0"}, state)
    assert out.messages == ({"t": "hello", "v": 1}, {"t": "state", "s": "listening"})
    assert any("0.1.0" in msg for _, msg in out.logs)


def test_ack_is_logged_and_changes_nothing():
    state = DeviceState(capture_active=True)
    out = handle_uplink({"t": "ack", "seq": 5}, state)
    assert out.state == state
    assert out.messages == ()
    assert any("5" in msg for _, msg in out.logs)


def test_unknown_type_is_logged_and_ignored():
    state = DeviceState()
    out = handle_uplink({"t": "nosuchtype", "whatever": True}, state)
    assert out.state == state
    assert out.messages == ()
    assert out.logs


def test_unknown_button_is_logged_and_ignored():
    out = handle_uplink({"t": "button", "b": "eject"}, DeviceState())
    assert out.capture_request is None
    assert out.logs


def test_every_up_fixture_line_is_accepted():
    """DoD 3, first half; DoD 4 for the unknown-field case."""
    state = DeviceState()
    for line in (FIXTURES / "display-protocol-v1-up.jsonl").read_text().splitlines():
        state = handle_uplink(json.loads(line), state).state


def test_successful_start_moves_to_listening():
    out = apply_capture_result(DeviceState(), requested=True, ok=True)
    assert out.state.screen == SCREEN_LISTENING
    assert out.state.capture_active is True
    assert out.messages == ({"t": "state", "s": "listening"},)


def test_failed_start_stays_idle_and_reports_offline():
    """Claiming `listening` when nothing is capturing would make the protocol lie."""
    out = apply_capture_result(DeviceState(), requested=True, ok=False)
    assert out.state.screen == SCREEN_IDLE
    assert out.state.capture_active is False
    assert out.messages == ({"t": "error", "text": "Recognition offline"},)


def test_a_failed_start_latches_offline_so_recovery_can_clear_it():
    """Unlatched, the watchdog repeats the error moments later, and nothing
    ever clears it: mark_online emits nothing when nothing was latched, so the
    panel sits on the error screen until someone presses start again."""
    out = apply_capture_result(DeviceState(), requested=True, ok=False)
    assert out.state.offline is True
    assert mark_offline(out.state).messages == ()
    assert mark_online(out.state).messages == ({"t": "state", "s": "idle"},)


def test_a_failed_re_assert_from_a_live_state_goes_idle():
    """The reconnect path. Recognition is not capturing, so `listening` would
    be the protocol lying about what the device is doing."""
    live = DeviceState(screen=SCREEN_LISTENING, capture_active=True,
                       tracking_status="clipped")
    out = apply_capture_result(live, requested=True, ok=False)
    assert out.state.screen == SCREEN_IDLE
    assert out.state.capture_active is False
    assert out.state.tracking_status == "ok"
    assert out.messages == ({"t": "error", "text": "Recognition offline"},)


def test_stop_goes_idle_even_when_the_post_fails():
    """Stop must not be conditional on the health of what it is stopping."""
    live = DeviceState(screen=SCREEN_ANALYZING, capture_active=True, tracking_status="clipped")
    out = apply_capture_result(live, requested=False, ok=False)
    assert out.state.screen == SCREEN_IDLE
    assert out.state.capture_active is False
    assert out.messages == ({"t": "state", "s": "idle"},)
    assert out.logs


def test_stop_resets_tracking_so_a_stale_error_is_not_suppressed():
    live = DeviceState(screen=SCREEN_LISTENING, capture_active=True, tracking_status="clipped")
    out = apply_capture_result(live, requested=False, ok=True)
    assert out.state.tracking_status == "ok"


def test_capture_result_preserves_the_mute_gate():
    out = apply_capture_result(DeviceState(muted=True), requested=True, ok=True)
    assert out.state.muted is True
