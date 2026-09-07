"""Headless capture: camera frames in, event dicts out. No HTTP in this file.

This composes the pieces S6 built — HolisticExtractor, AutoTake, SignRecogniser,
ClipStore, check_tracking — into a loop with no window and no keyboard, because
the board has neither. `experiments/live_demo.py` deliberately keeps its own
loop: it is the tool S7 records with, and destabilising it to save duplication
would be a bad trade mid-stage. See section 2 of the design for the convergence point.

`cv2` is imported inside `CameraSource.__init__` so this module can be imported
— and tested — without a camera, without OpenCV and without MediaPipe.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np

from islkit.infer import (
    AutoTake,
    ClipTooShort,
    Prediction,
    TakeQuality,
    Tracking,
    check_tracking,
    take_quality,
)
from recognition.view import annotate

log = logging.getLogger("recognition.pipeline")


class CameraUnavailable(RuntimeError):
    """The camera would not open, or opened and delivered nothing.

    A RuntimeError rather than SystemExit on purpose: live_demo can exit here
    because a person is watching it, but this is a device service and the
    caller's job is to emit a fault, back off and retry.
    """


# AutoTake's defaults were tuned by hand at this rate on a Mac. Everything below
# is expressed as a ratio against it rather than as seconds, so the conversion is
# exactly the identity here — seconds would reintroduce float drift (0.96 * 12.5
# is 12.000000000000002, and a ceil of that is 13).
REFERENCE_FPS = 12.5
_REFERENCE_COUNTS = {
    "pre_roll": 12,  # 0.96 s of leading rest seeded into every take
    "rest_to_arm": 6,  # 0.48 s at rest before we will accept a sign
    "raised_to_start": 6,  # 0.48 s raised before a take opens
    "rest_to_close": 8,  # 0.64 s at rest closes the take
    "max_frames": 400,  # 32 s runaway cap
}
# A one-frame run is a MediaPipe dropout, not a state change.
_FLOORS = {"pre_roll": 1, "max_frames": 30}
_DEFAULT_FLOOR = 2


@dataclass(frozen=True)
class TakeTiming:
    """AutoTake's thresholds at some particular frame rate."""

    pre_roll: int
    rest_to_arm: int
    raised_to_start: int
    rest_to_close: int
    max_frames: int

    def as_dict(self) -> dict:
        return asdict(self)


def derive_timing(fps: float) -> TakeTiming:
    """Scale AutoTake's Mac-tuned frame counts to the rate actually observed.

    At 3 fps a literal `rest_to_arm=6` becomes a two-second wait before the
    device will accept a sign, and a literal `max_frames=400` lets someone who
    raises their hands and holds still record for over two minutes.

    `min_frames` is deliberately absent. It is a resampling floor rather than a
    duration — `encode_clip` resamples instead of padding, so too few frames
    would classify without complaint — and picking a board value for it without
    the board's real rate would be a guess dressed as a decision.
    """
    ratio = max(fps, 0.1) / REFERENCE_FPS
    counts = {
        name: max(_FLOORS.get(name, _DEFAULT_FLOOR), round(reference * ratio))
        for name, reference in _REFERENCE_COUNTS.items()
    }
    return TakeTiming(**counts)


class FpsMeter:
    """Rolling frame rate over the last `window` frames.

    Reported rather than assumed: `/health` publishing a real measured rate is
    how a 2 fps board becomes visible instead of inferred.
    """

    def __init__(self, window: int = 60):
        self._times: deque[float] = deque(maxlen=window)

    def tick(self, now: float | None = None) -> None:
        self._times.append(time.perf_counter() if now is None else now)

    @property
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0


class FrameSource(Protocol):
    """Where frames come from. One implementation reads a camera, one is a test."""

    def read(self) -> np.ndarray | None:
        """The next frame, or None if the source is not delivering."""

    def close(self) -> None: ...

    def describe(self) -> dict:
        """Identity and geometry, for /health and for every take's sidecar."""


class CameraSource:
    """cv2.VideoCapture, with live_demo's hard-won opening discipline.

    `isOpened()` is not enough: a device can enumerate, report a resolution and
    still return nothing from every read() — which is what a disconnected or
    in-use webcam looks like. The only honest check is to pull a frame.
    """

    def __init__(self, index: int = 0, resolution: str = "1280x720"):
        import cv2

        self.index = index
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened() or not any(self._cap.read()[0] for _ in range(5)):
            self._cap.release()
            raise CameraUnavailable(f"camera {index} opens but returns no frames")

        want_w, want_h = (int(v) for v in resolution.lower().split("x"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        # Reported, never enforced. Aspect changes the encoded geometry, so takes
        # recorded at different aspects are not comparable — but refusing to start
        # over it would be worse than saying so.
        self.requested = (want_w, want_h)

    def read(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        self._cap.release()

    def describe(self) -> dict:
        return {
            "kind": "camera",
            "index": self.index,
            "width": self.width,
            "height": self.height,
            "aspect": round(self.width / self.height, 4) if self.height else 0.0,
            "requested": f"{self.requested[0]}x{self.requested[1]}",
        }


class FakeFrameSource:
    """Frames that are not pictures of anything, for tests.

    The pipeline hands whatever this returns straight to the extractor, and tests
    inject an extractor that ignores it, so the content is irrelevant and the
    shape is only there to keep `describe()` honest.
    """

    def __init__(self, n_frames: int | None = None, shape: tuple[int, int] = (4, 4)):
        self.n_frames = n_frames
        self.shape = shape
        self.served = 0
        self.closed = False

    def read(self) -> np.ndarray | None:
        if self.n_frames is not None and self.served >= self.n_frames:
            return None
        self.served += 1
        return np.zeros((*self.shape, 3), np.uint8)

    def close(self) -> None:
        self.closed = True

    def describe(self) -> dict:
        return {"kind": "fake", "height": self.shape[0], "width": self.shape[1]}


# check_tracking words its verdicts for a human ("no pose", "Hands not visible").
# The wire carries a stable machine enum instead: the English belongs to the
# orchestrator's phrase table, because it is display language and this repo must
# not be in the path of a wording or translation change.
TRACKING_STATUS = {
    "no pose": "absent",
    "clipped": "clipped",
    "no hands": "hands_hidden",
    "ok": "ok",
}


class TrackingDebouncer:
    """Turns per-frame tracking verdicts into rare, stable status changes.

    MediaPipe loses hands constantly — especially held still against the body —
    so an un-debounced status would flap every few frames and strobe whatever is
    rendering it. A new status has to hold before it is believed.
    """

    def __init__(self, hold_frames: int):
        self.hold_frames = max(1, hold_frames)
        self.status: str | None = None  # last status actually emitted
        self.raw: str | None = None  # what the current run is of
        self.run = 0  # how many consecutive frames it has held

    def update(self, tracking: Tracking) -> str | None:
        """Feed one frame's verdict. Returns a status only when it changes."""
        status = TRACKING_STATUS[tracking.status]
        self.run = self.run + 1 if status == self.raw else 1
        self.raw = status

        if status != self.status and self.run >= self.hold_frames:
            self.status = status
            return status
        return None


class RecognitionPipeline:
    """The capture loop. Camera in, event dicts out.

    `classify()` runs inline on this thread: it costs ~34 ms and happens once per
    deliberate gesture, so serialising it is free and keeps torch on exactly one
    thread. Events leave through `on_event`, which is called from this thread and
    must not block — the EventHub on the other end is built for that.
    """

    def __init__(
        self,
        recogniser,
        source_factory,
        on_event,
        *,
        extractor_factory=None,
        store=None,
        warmup_frames: int = 30,
        start_active: bool = False,
        classifier_name: str = "",
        classifier_sha256: str = "",
        min_free_mb: int = 500,
        drop_limit: int | None = None,
        reopen_backoff: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0),
        max_reopen_attempts: int | None = None,
        on_pause=None,
        view=None,
    ):
        self._recogniser = recogniser
        self._source_factory = source_factory
        self._on_event = on_event
        self._extractor_factory = extractor_factory or self._default_extractor
        self._store = store
        # Called with no arguments when capture pauses. The pipeline does not
        # know what an EventHub is — serve.py binds this to hub.clear_take so
        # the retained take-state slot doesn't outlive the take it describes.
        self._on_pause = on_pause
        self._warmup_frames = max(1, warmup_frames)
        self._classifier_name = classifier_name
        self._classifier_sha256 = classifier_sha256

        self.dominant = recogniser.dominant
        self._lock = threading.Lock()
        self._capture = start_active
        self._stop = threading.Event()

        self._source = None
        self._extractor = None
        self._fps = FpsMeter()
        self._timing: TakeTiming | None = None
        self._auto: AutoTake | None = None
        self._tracker: TrackingDebouncer | None = None
        self._take_state: str | None = None
        self._started = time.monotonic()
        self._frames_seen = 0
        self._min_free_mb = min_free_mb
        self._drop_limit = drop_limit
        self._reopen_backoff = reopen_backoff
        self._max_reopen_attempts = max_reopen_attempts
        self._fault: dict | None = None
        self._absent_disarmed = False
        # Optional ViewSink. None, or nobody watching, means the frame path
        # below costs one boolean read. cv2 stays lazily imported for the same
        # reason CameraSource defers it: this module must import without OpenCV.
        self._view = view
        self._cv2 = None
        self._drops = 0

    @staticmethod
    def _default_extractor():
        from islkit.infer import HolisticExtractor

        return HolisticExtractor()

    # -- control surface, called from the HTTP threads ---------------------

    def set_capture(self, active: bool) -> bool:
        """Returns the state now in force.

        Pausing disarms in the same locked step. Setting the flag alone would
        leave a half-finished take in the buffer, which then completes when
        capture resumes — a sign nobody made, attributed to whoever is in frame.
        """
        with self._lock:
            self._capture = bool(active)
            paused = not self._capture
            if paused and self._auto is not None:
                self._auto.disarm()
                self._take_state = None
            in_force = self._capture

        # Outside the lock deliberately: on_pause (bound to hub.clear_take)
        # must not be called while holding this non-reentrant lock, and
        # in_force is a local snapshot rather than a second read of
        # self._capture, which a concurrent set_capture() could have already
        # moved on from.
        if paused and self._on_pause is not None:
            self._on_pause()
        return in_force

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict:
        # Measured before the lock: self._store never changes after
        # construction, and a disk_usage() syscall has no business holding a
        # lock the capture thread also wants.
        disk_free_mb = self._measure_disk_free_mb()
        with self._lock:
            # Bound to locals rather than read twice (once for the truthiness
            # check, once for the attribute access): run() and _note_drop()
            # both set self._source/self._tracker to None from the capture
            # thread without this lock, so a poll landing between the two
            # reads used to see a live object turn into None mid-expression
            # and raise AttributeError instead of a clean 500.
            source = self._source
            tracker = self._tracker
            # Read off the live extractor rather than stored from the flag, so
            # this says what is in force rather than what was asked for. It is
            # the difference between a board running at 2 fps and one running
            # at 4, and it was invisible until it was published here.
            extractor = self._extractor
            return {
                "ok": self._fault is None,
                "capture": self._capture,
                "fps": round(self._fps.fps, 2),
                "classes": len(self._recogniser.label_map),
                "classifier": self._classifier_name,
                "sha256": self._classifier_sha256,
                "tracking": tracker.status if tracker else None,
                "state": self._take_state,
                "uptime_s": round(time.monotonic() - self._started, 1),
                "frames": self._frames_seen,
                "source": source.describe() if source else None,
                "autotake": self._timing.as_dict() if self._timing else None,
                "threads": getattr(extractor, "num_threads", None),
                "min_frames": self._recogniser.min_frames,
                "threshold": self._recogniser.threshold,
                "disk_free_mb": disk_free_mb,
            }

    # -- the loop ----------------------------------------------------------

    def run(self) -> None:
        """Loop until stop(). A camera or model failure is a fault and a retry,
        never an exit — that includes building the extractor itself. A missing
        MediaPipe asset or a bad wheel raises out of the factory exactly like a
        dead camera raises out of source_factory, and must be survived the same
        way: emit `fault model_error`, latch, back off, retry."""
        attempts = 0  # backoff-ladder position: resets on a healthy reopen
        total_attempts = 0  # give-up counter: never resets within one run()
        # The extractor gets its own ladder position and give-up counter,
        # independent of the source's: they open at different times (the
        # extractor once, up front; the source on every camera loss) and
        # sharing one counter would make an extractor retry eat into the
        # source's retry budget for no reason.
        ext_attempts = 0
        ext_total_attempts = 0
        try:
            while not self._stop.is_set():
                if self._extractor is None:
                    if self._max_reopen_attempts is not None:
                        if ext_total_attempts >= self._max_reopen_attempts:
                            return
                    ext_attempts += 1
                    ext_total_attempts += 1
                    try:
                        self._extractor = self._extractor_factory()
                        self._fault = None
                        ext_attempts = 0
                    except Exception as exc:  # noqa: BLE001 - a bad wheel or a
                        # missing model asset must not be able to exit this thread.
                        self._fault_out("model_error", f"{type(exc).__name__}: {exc}")
                        self._backoff(ext_attempts)
                        continue

                if self._source is None:
                    if self._max_reopen_attempts is not None:
                        if total_attempts >= self._max_reopen_attempts:
                            return
                    attempts += 1
                    total_attempts += 1
                    try:
                        self._source = self._source_factory()
                        self._fault = None
                        attempts = 0  # a healthy reopen must not pin the backoff ladder
                    except CameraUnavailable as exc:
                        self._fault_out("camera_open_failed", str(exc))
                        self._backoff(attempts)
                        continue

                try:
                    image = self._source.read()
                    if image is None:
                        if self._note_drop():
                            self._backoff(attempts)
                        continue

                    self._drops = 0
                    self._on_frame(image)
                except Exception as exc:  # noqa: BLE001 - nothing here may exit
                    # A cv2 read or a MediaPipe process() that starts raising has
                    # stopped working, same as a camera that stopped returning
                    # frames — reuse camera_lost rather than inventing a fourth code.
                    self._fault_out("camera_lost", f"{type(exc).__name__}: {exc}")
                    if self._auto is not None:
                        with self._lock:
                            self._auto.disarm()
                            self._take_state = None
                    self._close_source_quietly()
                    self._drops = 0
                    self._backoff(attempts)
                    continue
        finally:
            try:
                if self._extractor is not None:
                    self._extractor.close()
            finally:
                if self._source is not None:
                    self._source.close()

    def _note_drop(self) -> bool:
        """Returns True once the drop run is long enough to call the camera lost."""
        self._drops += 1
        limit = self._drop_limit if self._drop_limit is not None else self._current_drop_limit()
        if self._drops < limit:
            return False
        self._fault_out(
            "camera_lost",
            f"camera stopped returning frames after {self._frames_seen} frames",
        )
        if self._auto is not None:
            with self._lock:
                self._auto.disarm()
                self._take_state = None
        self._close_source_quietly()
        self._drops = 0
        return True

    def _close_source_quietly(self) -> None:
        """close() itself raising must not be able to exit run() either — the
        same governing rule as the read/process failure this is usually called
        from, just on a colder path."""
        source, self._source = self._source, None
        if source is None:
            return
        try:
            source.close()
        except Exception as exc:  # noqa: BLE001 - nothing here may exit
            log.warning("source.close() raised: %s", exc)

    def _current_drop_limit(self) -> int:
        """~5 s of frames, at whatever rate we are actually running.

        No fixed 30-frame floor: at 3 fps that was 10 s, twice section 8's budget. A
        small floor still guards a dropout from tripping the fault before the
        rate is even measured.
        """
        if not self._fps.fps:
            return 60
        return max(2, round(5.0 * self._fps.fps))

    def _fault_out(self, code: str, msg: str) -> None:
        self._fault = {"e": "fault", "code": code, "msg": msg}
        self._on_event(dict(self._fault))

    def _backoff(self, attempts: int) -> None:
        # attempts can be 0 right after a reset (see run()): treat that as "the
        # first attempt" rather than wrapping to reopen_backoff[-1] — the whole
        # point of the reset is that a fresh outage should not inherit the
        # previous outage's backoff ceiling.
        index = min(max(attempts, 1) - 1, len(self._reopen_backoff) - 1)
        delay = self._reopen_backoff[index]
        if delay:
            self._stop.wait(delay)

    def _on_frame(self, image) -> None:
        frame, raw, results = self._extractor.process(image)
        self._fps.tick()
        self._frames_seen += 1

        # Before the warmup return and before the capture gate, both
        # deliberately: the view's whole purpose is aiming the camera, which
        # happens while warming up and while paused. The overlay's tracking
        # status is therefore one frame behind — immaterial at ~2 fps, and the
        # alternative is publishing from three separate places.
        self._publish_view(frame, results)

        if self._auto is None:
            if self._frames_seen < self._warmup_frames:
                return
            self._arm_segmenter()

        self._emit_tracking(check_tracking(raw, self.dominant))
        self._disarm_if_absent()

        with self._lock:
            if not self._capture:
                return
            take = self._auto.update(raw)
            state = self._auto.state
            previous = self._take_state
            n_frames = self._auto.n_frames
            self._take_state = state

        if take is not None:
            self._classify(take)
        self._emit_take_state(state, previous, n_frames)

    def _publish_view(self, frame, results) -> None:
        """Hand an annotated frame to the debug view, if one is being watched.

        Reuses the landmarks recognition already extracted — this runs no
        inference. Failures are swallowed inside `annotate`: a debug view is
        never worth taking the capture loop down.
        """
        if self._view is None or not self._view.watching:
            return
        if self._cv2 is None:
            import cv2

            self._cv2 = cv2
        tracker = self._tracker
        jpeg = annotate(
            self._cv2,
            self._extractor,
            frame,
            results,
            {
                "tracking": tracker.status if tracker else None,
                "take_state": self._take_state,
                "capture": self._capture,
                "fps": self._fps.fps,
            },
        )
        if jpeg is not None:
            self._view.publish(jpeg)

    def _disarm_if_absent(self) -> None:
        """Nobody in frame means no take.

        AutoTake reads an untracked hand as rest — correct for its purpose, since
        MediaPipe drops hands constantly — but it means an empty room accumulates
        rest frames and leaves the segmenter armed at nobody. Worse, the first
        hand to appear after that would open a take with an empty pre-roll.
        """
        if self._tracker.raw != "absent":
            self._absent_disarmed = False
            return
        if not self._absent_disarmed and self._tracker.run >= self._tracker.hold_frames:
            with self._lock:
                self._auto.disarm()
                self._take_state = None
            self._absent_disarmed = True

    def _arm_segmenter(self) -> None:
        """Size AutoTake against the rate this machine actually achieved."""
        self._timing = derive_timing(self._fps.fps)
        self._auto = AutoTake(
            dominant=self.dominant,
            pre_roll=self._timing.pre_roll,
            rest_to_arm=self._timing.rest_to_arm,
            raised_to_start=self._timing.raised_to_start,
            rest_to_close=self._timing.rest_to_close,
            max_frames=self._timing.max_frames,
        )
        # ~0.5 s of agreement before a framing change is believed.
        self._tracker = TrackingDebouncer(hold_frames=max(2, self._timing.rest_to_arm))

    def _emit_tracking(self, tracking: Tracking) -> None:
        status = self._tracker.update(tracking)
        if status is not None:
            self._on_event(
                {
                    "e": "tracking",
                    "status": status,
                    "hands": tracking.hands,
                    "clipped": tracking.clipped,
                }
            )

    def _emit_take_state(self, state: str, previous: str | None, n_frames: int) -> None:
        """`recording` fires every frame; `armed` only on the transition into it."""
        if state == "recording":
            self._on_event({"e": "recording", "frames": n_frames})
        elif state == "armed" and previous != "armed":
            self._on_event({"e": "armed"})

    def _classify(self, take: list) -> None:
        self._on_event({"e": "classifying"})
        prediction: Prediction | None = None
        try:
            prediction = self._recogniser.classify(take)
        except ClipTooShort:
            self._on_event(
                {
                    "e": "unclear",
                    "conf": 0.0,
                    "top3": [],
                    "reason": "too_short",
                    "n_frames": len(take),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one bad take must not end the service
            self._fault_out("model_error", f"{type(exc).__name__}: {exc}")
        else:
            # A watchdog polls /health: one bad take must not mark the service
            # down forever once a later take proves the model still works.
            self._fault = None

        quality = take_quality(take, self.dominant)
        if prediction is not None:
            self._on_event(self._prediction_event(prediction, quality))
        self._save(take, prediction, quality)

    def _prediction_event(self, prediction: Prediction, quality: TakeQuality) -> dict:
        top3 = [[gloss, float(p)] for gloss, p in prediction.top3]
        if prediction.gloss is None:
            return {"e": "unclear", "conf": float(prediction.confidence), "top3": top3}
        return {
            "e": "recognised",
            "gloss": prediction.gloss,
            "conf": float(prediction.confidence),
            "top3": top3,
            "n_frames": prediction.n_frames,
            "usable_frames": prediction.usable_frames,
            "encode_ms": round(prediction.encode_ms, 1),
            "forward_ms": round(prediction.forward_ms, 1),
            # The difference between "the model was wrong" and "the camera never
            # saw you". Invisible in a gloss, and S6 proved you need it.
            "take_usable": quality.usable,
        }

    def _measure_disk_free_mb(self) -> int | None:
        """None if there is no store to measure, or the measurement itself fails.

        Shared by `_save` (which gates on it) and `health()` (which can then
        warn about a full disk before any gesture has ever been recorded) so
        the two never compute it two different ways.
        """
        if self._store is None:
            return None
        try:
            root = self._store.unlabelled_root
            while not root.exists() and root != root.parent:
                root = root.parent
            usage = shutil.disk_usage(root)
            return usage.free // (1024 * 1024)
        except OSError:
            # A disk_usage() failure is not a camera problem — it must not
            # surface as camera_lost and force a pointless reopen.
            return None

    def _save(self, take: list, prediction: Prediction | None, quality: TakeQuality) -> None:
        if self._store is None:
            return
        disk_free_mb = self._measure_disk_free_mb()
        if disk_free_mb is not None and disk_free_mb < self._min_free_mb:
            return  # keep recognising; stop writing. /health says why.
        try:
            self._store.save(
                take,
                None,  # nobody is at a keyboard to label this
                prediction,
                self._recogniser.threshold,
                meta={
                    "trigger": "auto",
                    "source": "service",
                    "take_usable": quality.usable,
                    "take_hand_fraction": round(quality.hand_fraction, 3),
                    "classifier": self._classifier_name,
                    "classifier_sha256": self._classifier_sha256,
                    # build_dataset re-encodes from raw landmarks, but a take is
                    # only interpretable later against the encoder that
                    # classified it.
                    "encoder": getattr(self._recogniser, "meta", {}).get("encoder"),
                    "fps": round(self._fps.fps, 2),
                    **(self._source.describe() if self._source else {}),
                },
            )
        except OSError as exc:
            # A failed recording is not a recognition failure and must not take
            # the service down with it.
            log.warning("failed to save take: %s", exc)
