import json
from pathlib import Path

import pytest

from orchestrator.protocol import MAX_LINE, ProtocolError, cap_text, encode

FIXTURES = Path(__file__).parent / "fixtures"


def test_every_down_fixture_line_round_trips_byte_for_byte():
    """DoD 2: the encoder must reproduce the fixture exactly.

    The fixture is written compact and key-ordered; json.loads preserves
    insertion order, so a correct encoder is byte-identical.
    """
    for line in (FIXTURES / "display-protocol-v1-down.jsonl").read_text().splitlines():
        assert encode(json.loads(line)) == (line + "\n").encode("utf-8")


def test_encode_is_compact_and_newline_terminated():
    assert encode({"t": "state", "seq": 1, "s": "idle"}) == b'{"t":"state","seq":1,"s":"idle"}\n'


def test_encode_rejects_a_line_over_the_frame_limit():
    with pytest.raises(ProtocolError, match="256"):
        encode({"t": "error", "text": "x" * 400})


def test_encode_allows_a_line_exactly_at_the_limit():
    filler = "x" * (MAX_LINE - len(b'{"t":"error","text":""}\n'))
    assert len(encode({"t": "error", "text": filler})) == MAX_LINE


def test_cap_text_truncates_at_120_characters():
    assert len(cap_text("y" * 500)) == 120


def test_cap_text_leaves_short_text_untouched():
    assert cap_text("I have pain") == "I have pain"


def test_encode_keeps_non_ascii_as_utf8_not_escapes():
    assert "\\u" not in encode({"t": "error", "text": "café"}).decode("utf-8")
