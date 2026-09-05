from pathlib import Path

from orchestrator.core import SCREEN_ANALYZING, DeviceState, translate
from orchestrator.phrases import PhraseTable

TABLE = PhraseTable.load(Path(__file__).resolve().parents[1] / "phrases.json")
ANALYZING = DeviceState(screen=SCREEN_ANALYZING, capture_active=True)

RECOGNISED = {
    "e": "recognised", "gloss": "hello", "conf": 0.92,
    "top3": [["hello", 0.92], ["howareyou", 0.04]],
    "n_frames": 31, "usable_frames": 30,
    "encode_ms": 11.6, "forward_ms": 21.6, "take_usable": True,
}


def test_a_known_gloss_becomes_a_result_and_an_audio_intent():
    out = translate(RECOGNISED, ANALYZING, TABLE)
    assert out.messages == ({"t": "result", "id": "hello", "text": "Hello", "conf": 0.92},)
    assert out.audio == ("hello",)


def test_a_result_does_not_change_the_screen():
    """The next `armed` carries the device back to listening, not this."""
    out = translate(RECOGNISED, ANALYZING, TABLE)
    assert out.state.screen == SCREEN_ANALYZING


def test_an_unknown_gloss_becomes_unclear_with_no_audio():
    """The 262-class head returns 'truck' for hello. Common, not an edge case."""
    event = dict(RECOGNISED, gloss="truck", conf=0.81)
    out = translate(event, ANALYZING, TABLE)
    assert out.messages == ({"t": "unclear", "conf": 0.81},)
    assert out.audio == ()
    assert any("truck" in msg for _, msg in out.logs)


def test_an_unusable_take_still_emits_the_result_and_logs():
    out = translate(dict(RECOGNISED, take_usable=False), ANALYZING, TABLE)
    assert out.messages[0]["t"] == "result"
    assert any("take_usable" in msg for _, msg in out.logs)


def test_low_confidence_unclear():
    event = {"e": "unclear", "conf": 0.35, "top3": [["religion", 0.35]]}
    out = translate(event, ANALYZING, TABLE)
    assert out.messages == ({"t": "unclear", "conf": 0.35},)
    assert out.audio == ()


def test_too_short_unclear_carries_no_extra_protocol_fields():
    """The two unclear shapes differ; reason and n_frames are logged, not sent."""
    event = {"e": "unclear", "conf": 0.0, "top3": [], "reason": "too_short", "n_frames": 5}
    out = translate(event, ANALYZING, TABLE)
    assert out.messages == ({"t": "unclear", "conf": 0.0},)
    assert any("too_short" in msg for _, msg in out.logs)


def test_each_fault_code_becomes_an_error_carrying_its_message():
    for code, text in (
        ("camera_lost", "camera 2 stopped returning frames"),
        ("camera_open_failed", "camera 2 opens but returns no frames"),
        ("model_error", "classify() raised RuntimeError"),
    ):
        out = translate({"e": "fault", "code": code, "msg": text}, ANALYZING, TABLE)
        assert out.messages == ({"t": "error", "text": text},)
        assert any(code in msg for _, msg in out.logs)


def test_a_fault_is_reported_even_while_not_capturing():
    """Everything else is gated by the idle gate. A fault is not."""
    out = translate({"e": "fault", "code": "camera_lost", "msg": "gone"}, DeviceState(), TABLE)
    assert out.messages == ({"t": "error", "text": "gone"},)


def test_fault_text_is_capped_at_the_protocol_limit():
    out = translate({"e": "fault", "code": "model_error", "msg": "z" * 400}, ANALYZING, TABLE)
    assert len(out.messages[0]["text"]) == 120


def test_a_long_phrase_text_is_capped():
    from orchestrator.phrases import Phrase
    from orchestrator.phrases import PhraseTable as PT

    table = PT({"x": Phrase(gloss="x", text="w" * 400, audio="a.wav")})
    out = translate({"e": "recognised", "gloss": "x", "conf": 0.5}, ANALYZING, table)
    assert len(out.messages[0]["text"]) == 120
