from pathlib import Path
from unittest import mock

from orchestrator.audio import Player
from orchestrator.phrases import Phrase, PhraseTable

TABLE = PhraseTable({
    "hello": Phrase(gloss="hello", text="Hello", audio="audio/en/hello.wav"),
})


def make_player(tmp_path, runner, wav=True):
    if wav:
        wav_path = tmp_path / "audio" / "en" / "hello.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(b"RIFF")
    return Player(TABLE, tmp_path, command=["fakeplay"], runner=runner)


def test_playing_a_known_gloss_invokes_the_player_with_the_wav_path(tmp_path):
    calls = []
    player = make_player(tmp_path, calls.append)
    player.play("hello", muted=False)
    player.close()
    assert calls == [["fakeplay", str(tmp_path / "audio" / "en" / "hello.wav")]]
    assert player.played == 1


def test_muted_plays_nothing(tmp_path):
    calls = []
    player = make_player(tmp_path, calls.append)
    player.play("hello", muted=True)
    player.close()
    assert calls == []
    assert player.skipped == 1


def test_a_missing_wav_is_skipped_not_raised(tmp_path):
    calls = []
    player = make_player(tmp_path, calls.append, wav=False)
    player.play("hello", muted=False)
    player.close()
    assert calls == []
    assert player.skipped == 1


def test_a_stat_error_on_the_wav_is_skipped_not_raised(tmp_path):
    calls = []
    player = make_player(tmp_path, calls.append)
    with mock.patch("pathlib.Path.is_file", side_effect=PermissionError("access denied")):
        player.play("hello", muted=False)
    player.close()
    assert calls == []
    assert player.skipped == 1


def test_an_unknown_gloss_is_skipped(tmp_path):
    calls = []
    player = make_player(tmp_path, calls.append)
    player.play("truck", muted=False)
    player.close()
    assert calls == []
    assert player.skipped == 1


def test_a_failing_player_never_takes_the_process_down(tmp_path):
    def boom(argv):
        raise OSError("aplay: no such device")

    player = make_player(tmp_path, boom)
    player.play("hello", muted=False)
    player.close()
    assert player.failed == 1


def test_play_returns_before_the_runner_finishes(tmp_path):
    """A two-second wav must not stall event handling."""
    import threading
    import time

    release = threading.Event()
    started = threading.Event()

    def slow(argv):
        started.set()
        release.wait(5)

    player = make_player(tmp_path, slow)
    start = time.monotonic()
    player.play("hello", muted=False)
    elapsed = time.monotonic() - start
    assert started.wait(2)
    assert elapsed < 0.5, f"play() took {elapsed}s, expected < 0.5s"
    release.set()
    player.close()


def test_close_is_idempotent(tmp_path):
    player = make_player(tmp_path, lambda argv: None)
    player.close()
    player.close()
