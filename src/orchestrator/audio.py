"""The audio path. Nothing in here may raise into the caller.

This board currently has no reachable audio output (AUDIO-SPIKE-RESULTS.md), so
the expected outcome on hardware today is silence with a clean log. The design
requirement is unchanged either way: a missing file or a failing player is
logged and skipped, and never takes the process down.

Playback runs on its own thread so a two-second wav cannot stall event handling.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .phrases import PhraseTable

log = logging.getLogger(__name__)

DEFAULT_COMMAND = ["aplay", "-q"]
PLAY_TIMEOUT_S = 10.0
QUEUE_MAXSIZE = 8

_STOP = object()


def default_runner(argv: list[str]) -> None:
    subprocess.run(argv, check=True, timeout=PLAY_TIMEOUT_S, capture_output=True)


class Player:
    def __init__(
        self,
        phrases: PhraseTable,
        base_dir: Path,
        command: list[str] | None = None,
        runner: Callable[[list[str]], None] | None = None,
    ):
        self._phrases = phrases
        self._base_dir = Path(base_dir)
        self._command = list(command or DEFAULT_COMMAND)
        self._runner = runner or default_runner
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._closed = False
        self.played = 0
        self.skipped = 0
        self.failed = 0
        self._thread = threading.Thread(target=self._loop, name="audio", daemon=True)
        self._thread.start()

    def play(self, gloss: str, muted: bool) -> None:
        """Enqueue and return. Every failure path here is a log line."""
        if muted:
            self.skipped += 1
            log.info("muted; not playing %r", gloss)
            return
        phrase = self._phrases.get(gloss)
        if phrase is None:
            self.skipped += 1
            log.warning("no phrase row for %r; nothing to play", gloss)
            return
        path = self._base_dir / phrase.audio
        if not path.is_file():
            self.skipped += 1
            log.warning("audio file missing, skipping: %s", path)
            return
        try:
            self._queue.put_nowait(self._command + [str(path)])
        except queue.Full:
            self.skipped += 1
            log.warning("audio queue full; dropping %r", gloss)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._thread.join(timeout=PLAY_TIMEOUT_S + 1)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._runner(item)
                self.played += 1
            except Exception as exc:  # noqa: BLE001 - nothing here may propagate
                self.failed += 1
                log.warning("player failed for %s: %r", item[-1], exc)
