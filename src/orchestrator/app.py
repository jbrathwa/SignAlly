"""The mediator: owns DeviceState and the lock, and nothing else.

Every decision lives in `core`. This module's whole job is to hold the current
state, serialise access to it, and turn the core's Outcomes into publishes and
audio calls.
"""

from __future__ import annotations

import logging
import threading
import time

from . import core
from .core import DeviceState

log = logging.getLogger(__name__)

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}

# Outstanding seqs are bounded. The panel is not obliged to ack — the courier
# rewrite that would make it do so is a follow-up — so an unbounded set would
# grow for as long as the process runs.
OUTSTANDING_MAX = 256


class Orchestrator:
    def __init__(self, phrases, hub, player, upstream):
        self._phrases = phrases
        self._hub = hub
        self._player = player
        self._upstream = upstream
        self._lock = threading.Lock()
        self._state = DeviceState()
        self._started_at = time.monotonic()
        self._acks = 0
        self._acks_unknown = 0
        self._outstanding: dict[int, None] = {}  # emitted seqs, oldest first

    @property
    def state(self) -> DeviceState:
        with self._lock:
            return self._state

    @property
    def hub(self):
        return self._hub

    def _apply(self, outcome) -> None:
        """Caller holds the lock. Publish, then play.

        `core.handle_uplink` returns a `UplinkOutcome`, which has no `audio`
        field (only `core.Outcome` does). Dispatch on type explicitly rather
        than `getattr(outcome, "audio", ())` — a future rename or typo of
        `Outcome.audio` must raise, not silently stop playing audio.
        """
        self._state = outcome.state
        for level, message in outcome.logs:
            log.log(_LEVELS.get(level, logging.INFO), "%s", message)
        for message in outcome.messages:
            self._track(self._hub.publish(message))
        if isinstance(outcome, core.Outcome):
            for gloss in outcome.audio:
                self._player.play(gloss, muted=outcome.state.muted)

    def _track(self, stamped: dict) -> None:
        """Caller holds the lock. Hold a `seq` open until the panel acks it."""
        seq = stamped.get("seq")
        if seq is None:
            return  # `hello` is not stamped and is never acked
        self._outstanding[seq] = None
        while len(self._outstanding) > OUTSTANDING_MAX:
            aged = next(iter(self._outstanding))
            del self._outstanding[aged]
            log.debug("seq %d aged out of the outstanding set unacked", aged)

    def _match_ack(self, seq) -> None:
        """Caller holds the lock. section 5: an ack is *matched* against emitted seq.

        Counting acks is not matching them. To a counter, a panel acking one
        seq eleven times and a panel acking eleven distinct seqs are the same
        thing, an ack for a line never sent is invisible, and `sent - received`
        can go negative. Discarding from the outstanding set instead makes each
        of those show up as what it is.
        """
        self._acks += 1
        if not isinstance(seq, int) or isinstance(seq, bool):
            self._acks_unknown += 1
            log.warning("ack carrying a non-integer seq %r", seq)
            return
        if seq == 0:
            # `hello` carries no seq, and the panel firmware answers it with an
            # ack of seq 0 — its own expected_flow.txt documents the convention.
            # Our counter starts at 1, so 0 is never one of ours; counting it as
            # an anomaly would tick on every panel boot and drown the signal the
            # counter exists for.
            log.debug("ack seq 0 — the panel's hello ack")
            return
        if seq in self._outstanding:
            del self._outstanding[seq]
            return
        if 1 <= seq <= self._hub.seq:
            log.debug("ack for seq %d, already matched or aged out", seq)
        else:
            self._acks_unknown += 1
            log.warning("ack for seq %d, which was never emitted (highest is %d)",
                        seq, self._hub.seq)

    def on_recognition_event(self, event: dict) -> None:
        with self._lock:
            self._apply(core.translate(event, self._state, self._phrases))

    def on_offline(self) -> None:
        with self._lock:
            self._apply(core.mark_offline(self._state))

    def on_connect(self) -> None:
        """Recognition's capture flag does not survive its restart; re-assert it.

        The re-assert's result is folded back through the core rather than
        discarded. A stream that reconnects while `POST /capture` fails means
        recognition is *not* capturing, and holding `capture_active=True` and
        `screen="listening"` locally would have the panel claim the device is
        listening while nothing is — the same objection section 3 raises against the
        start button.

        On success there is nothing to fold: local state already says exactly
        what recognition has just been told, and `apply_capture_result` would
        only spend a `seq` re-stating it. On failure `mark_online` is skipped,
        because it would clear the `offline` latch `apply_capture_result` has
        just set and take the error off the screen the panel needs it on.

        This POST is synchronous on the reader thread. It is affordable here
        only because it now runs once per genuine outage rather than on every
        reconnect; the events it delays sit in the TCP buffer, not on the floor.
        """
        with self._lock:
            wanted = self._state.capture_active
        ok = self._upstream.set_capture(True) if wanted else True
        with self._lock:
            if ok:
                self._apply(core.mark_online(self._state))
            else:
                self._apply(core.apply_capture_result(self._state, True, ok))

    def on_uplink(self, msg: dict) -> None:
        with self._lock:
            outcome = core.handle_uplink(msg, self._state)
            if msg.get("t") == "ack":
                self._match_ack(msg.get("seq"))
            self._apply(outcome)
            request = outcome.capture_request
        if request is None:
            return
        ok = self._upstream.set_capture(request)
        with self._lock:
            self._apply(core.apply_capture_result(self._state, request, ok))

    def health(self) -> dict:
        with self._lock:
            state = self._state
            outstanding = len(self._outstanding)
            received, unknown = self._acks, self._acks_unknown
        return {
            "ok": self._upstream.connected and not state.offline,
            "recognition_connected": self._upstream.connected,
            "last_line_age_s": round(self._upstream.last_line_age_s, 2),
            "screen": state.screen,
            "capture": state.capture_active,
            "muted": state.muted,
            "tracking": state.tracking_status,
            "seq": self._hub.seq,
            "subscribers": self._hub.subscribers,
            "dropped": self._hub.dropped,
            "acks": {"sent": self._hub.sent, "received": received, "unknown": unknown},
            "unmatched_acks": outstanding,
            "phrases": len(self._phrases),
            "audio": {"played": self._player.played, "skipped": self._player.skipped,
                      "failed": self._player.failed},
            "uptime_s": round(time.monotonic() - self._started_at, 1),
        }
