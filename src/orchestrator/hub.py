"""Fan-out to the /events subscribers, and the one place `seq` is assigned.

Modelled on the recognition service's EventHub, with one deliberate difference:
there, dropping is expected, because a camera thread must never stall. Here the
rate is roughly one message per second and a queue should never fill — a
non-zero `dropped` is a defect signal, not normal operation, which is why
/health reports it.
"""

from __future__ import annotations

import queue
import threading

MAX_SUBSCRIBERS = 4
QUEUE_MAXSIZE = 256


class EventHub:
    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS, maxsize: int = QUEUE_MAXSIZE):
        self._max_subscribers = max_subscribers
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._queues: list[queue.Queue] = []
        self._retained: dict | None = None
        self._seq = 0
        self._dropped = 0
        self._sent = 0

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._queues)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def sent(self) -> int:
        with self._lock:
            return self._sent

    def publish(self, msg: dict) -> dict:
        """Stamp `seq`, retain if it is state, fan out. Never raises."""
        with self._lock:
            if msg.get("t") == "hello":
                stamped = dict(msg)
            else:
                self._seq += 1
                rest = {k: v for k, v in msg.items() if k != "t"}
                stamped = {"t": msg["t"], "seq": self._seq, **rest}
                self._sent += 1
            if stamped.get("t") == "state":
                self._retained = stamped
            for q in self._queues:
                self._put(q, stamped)
            return stamped

    def subscribe(self) -> queue.Queue | None:
        """A queue seeded with hello and the current state, or None if full."""
        with self._lock:
            if len(self._queues) >= self._max_subscribers:
                return None
            # Reserve slots for seed messages (hello + retained state if present)
            # so maxsize keeps meaning real-message capacity for the caller.
            seeds = 1 + (1 if self._retained is not None else 0)
            q: queue.Queue = queue.Queue(maxsize=self._maxsize + seeds)
            self._put(q, {"t": "hello", "v": 1})
            if self._retained is not None:
                self._put(q, self._retained)
            self._queues.append(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def _put(self, q: queue.Queue, msg: dict) -> None:
        """Caller holds the lock. Drops the oldest rather than blocking."""
        try:
            q.put_nowait(msg)
        except queue.Full:
            try:
                q.get_nowait()
                self._dropped += 1
                q.put_nowait(msg)
            except (queue.Empty, queue.Full):
                self._dropped += 1
