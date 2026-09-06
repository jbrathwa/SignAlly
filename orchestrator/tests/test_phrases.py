import json
from pathlib import Path

import pytest

from orchestrator.phrases import PhraseTable, PhraseTableError

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


def test_table_key_set_equals_vocab_exactly():
    """A gloss with no row is a recognised sign the device cannot say."""
    vocab = set(json.loads((FIXTURES / "vocab.json").read_text()))
    table = PhraseTable.load(REPO / "phrases.json")
    assert table.glosses() == vocab


def test_get_returns_text_and_audio_for_a_known_gloss():
    table = PhraseTable.load(REPO / "phrases.json")
    phrase = table.get("doctor")
    assert phrase.gloss == "doctor"
    assert phrase.text == "I need a doctor"
    assert phrase.audio == "audio/en/doctor.wav"


def test_get_returns_none_for_an_unknown_gloss():
    """The 262-class head emits glosses like 'truck'. That is the common case."""
    table = PhraseTable.load(REPO / "phrases.json")
    assert table.get("truck") is None


def test_every_text_is_within_the_protocol_cap():
    table = PhraseTable.load(REPO / "phrases.json")
    for gloss in table.glosses():
        assert len(table.get(gloss).text) <= 120, gloss


def test_missing_file_refuses_to_start(tmp_path):
    with pytest.raises(PhraseTableError, match="nope.json"):
        PhraseTable.load(tmp_path / "nope.json")


def test_malformed_json_refuses_to_start(tmp_path):
    bad = tmp_path / "phrases.json"
    bad.write_text("{not json")
    with pytest.raises(PhraseTableError):
        PhraseTable.load(bad)


def test_missing_language_refuses_to_start(tmp_path):
    bad = tmp_path / "phrases.json"
    bad.write_text(json.dumps({"hello": {"text": {"en": "Hello"}, "audio": {}}}))
    with pytest.raises(PhraseTableError, match="hello"):
        PhraseTable.load(bad)
