"""Tests for the headless recognition pipeline.

Nothing here needs a camera, and nothing needs MediaPipe or torch: the frame
source and the extractor are both injected. What is pinned is the event stream,
because it is the interface the orchestrator is coded against and a silent
change to it fails invisibly on the other side of a socket.
"""

from __future__ import annotations

import numpy as np

from islkit.features import RawFrame
from islkit.infer import ClipTooShort, Prediction, check_tracking
from recognition.pipeline import (
    DROPOUT_FLOOR,
    MIN_FRAMES_CEILING,
    MIN_FRAMES_FLOOR,
    REFERENCE_FPS,
    CameraUnavailable,
    FakeFrameSource,
    FpsMeter,
    RecognitionPipeline,
    TrackingDebouncer,
    derive_min_frames,
    derive_timing,
)


def test_fake_source_yields_then_reports_exhaustion():
    source = FakeFrameSource(n_frames=2)
    assert isinstance(source.read(), np.ndarray)
    assert isinstance(source.read(), np.ndarray)
    assert source.read() is None  # exhausted reads as a dead camera


def test_fake_source_is_unbounded_when_n_frames_is_none():
    source = FakeFrameSource()
    assert all(source.read() is not None for _ in range(1000))


def test_fake_source_describes_itself():
    described = FakeFrameSource(shape=(8, 6)).describe()
    assert described["kind"] == "fake"
    assert (described["width"], described["height"]) == (6, 8)


def test_derivation_is_the_identity_at_the_rate_the_constants_were_tuned_at():
    """The regression that matters. AutoTake's defaults are pre_roll=12,
    rest_to_arm=6, raised_to_start=6, rest_to_close=8, max_frames=400 at
    12.5 fps; the conversion must reproduce them exactly, or every take on the
    Mac silently changes shape."""
    timing = derive_timing(REFERENCE_FPS)
    assert (timing.pre_roll, timing.rest_to_arm) == (12, 6)
    assert (timing.raised_to_start, timing.rest_to_close) == (6, 8)
    assert timing.max_frames == 400


def test_a_slow_board_gets_proportionally_smaller_counts():
    """At 3 fps a literal rest_to_arm=6 is a two-second wait before arming, and
    a literal max_frames=400 lets a stationary signer record for 133 seconds."""
    timing = derive_timing(3.0)
    assert timing.pre_roll == 3  # round(12 * 0.24)
    assert timing.max_frames == 96  # round(400 * 0.24)


def test_rest_to_close_does_not_scale_below_the_dropout_floor():
    """The one count that must NOT shrink with the frame rate.

    AutoTake reads an untracked hand as rest, so a run of dropped frames that
    reaches rest_to_close ends the take. Scaling 8 frames down by rate gives 2
    at the board's ~3 fps, and dropout runs measured on the board are 1-5 frames
    long — so 85.7% of dropouts closed a take mid-sign and 32% of takes came
    back too_short. Dropout length is a frame count that does not shrink with
    the rate, so this floor does not either.
    """
    assert derive_timing(3.0).rest_to_close == DROPOUT_FLOOR
    assert derive_timing(0.5).rest_to_close == DROPOUT_FLOOR
    # Inert where the constants were tuned: 8 was already above the floor.
    assert derive_timing(REFERENCE_FPS).rest_to_close == 8


def test_min_frames_follows_the_rate_between_its_bounds():
    """A fixed 8 is 2.5 s at 3 fps — longer than many real signs.

    Bounded at both ends on purpose: never above the hand-tuned 8, and never
    below 4, where there is too little trajectory left for encode_clip to
    resample honestly.
    """
    assert derive_min_frames(REFERENCE_FPS) == MIN_FRAMES_CEILING
    assert derive_min_frames(3.0) == MIN_FRAMES_FLOOR
    assert derive_min_frames(0.1) == MIN_FRAMES_FLOOR
    assert MIN_FRAMES_FLOOR <= derive_min_frames(5.0) <= MIN_FRAMES_CEILING


def test_run_lengths_never_fall_below_two_frames():
    """A one-frame run is noise, not a state change — MediaPipe drops hands
    constantly. Arming or closing on a single frame would make the segmenter
    fire on dropouts."""
    timing = derive_timing(0.5)
    assert timing.rest_to_arm >= 2
    assert timing.raised_to_start >= 2
    assert timing.rest_to_close >= DROPOUT_FLOOR
    assert timing.pre_roll >= 1
    assert timing.max_frames >= 30


def test_timing_is_reportable():
    assert derive_timing(REFERENCE_FPS).as_dict() == {
        "pre_roll": 12,
        "rest_to_arm": 6,
        "raised_to_start": 6,
        "rest_to_close": 8,
        "max_frames": 400,
    }


def test_fps_meter_is_zero_before_it_has_two_ticks():
    meter = FpsMeter()
    assert meter.fps == 0.0
    meter.tick()
    assert meter.fps == 0.0


def test_fps_meter_measures_the_rate():
    meter = FpsMeter()
    for i in range(11):
        meter.tick(now=100.0 + i * 0.25)  # 4 fps
    assert meter.fps == 4.0


def test_fps_meter_forgets_old_frames():
    """A rate that changes must be reflected, not averaged over all history."""
    meter = FpsMeter(window=5)
    for i in range(5):
        meter.tick(now=100.0 + i * 1.0)  # 1 fps
    for i in range(5):
        meter.tick(now=105.0 + i * 0.1)  # 10 fps
    assert meter.fps > 5.0


SHOULDER_Y, L_X, R_X = 0.30, 0.35, 0.65
BODY_SCALE = R_X - L_X


def make_frame(wrist_y: float | None, clipped: bool = False) -> RawFrame:
    """A synthetic frame whose dominant wrist sits at a chosen body-frame height.

    encode_frame puts the body-frame origin at the shoulder midpoint and scales
    by shoulder width, so wrist_y here is exactly what _wrist_height reads back —
    which is what AutoTake segments on. `wrist_y=None` means no hand tracked.
    """
    pose = np.zeros((33, 4), np.float32)
    pose[:, :2] = 0.5
    pose[11, :2] = (L_X, SHOULDER_Y)
    pose[12, :2] = (R_X, SHOULDER_Y)
    pose[23, :2] = (0.45, 0.95)
    pose[24, :2] = (0.55, 0.95)
    pose[:, 3] = 1.0
    if clipped:
        pose[11, :2] = (0.005, SHOULDER_Y)

    hand = None
    if wrist_y is not None:
        hand = np.zeros((21, 3), np.float32)
        hand[:, 0] = 0.60
        hand[:, 1] = SHOULDER_Y + wrist_y * BODY_SCALE
        hand[9] = hand[0] + np.array([0.02, 0.02, 0.0], np.float32)

    return RawFrame(
        pose=pose,
        face=np.full((478, 3), 0.5, np.float32),
        hand_left=None,
        hand_right=hand,
    )


ABSENT = RawFrame(pose=None, face=None, hand_left=None, hand_right=None)


def test_make_frame_places_the_wrist_where_it_says_it_does():
    """Pins the test helper itself. Every segmentation test below is meaningless
    if this drifts, and it would drift silently."""
    from islkit.infer import _wrist_height

    assert _wrist_height(make_frame(1.5), "right") == 1.5
    assert _wrist_height(make_frame(0.2), "right") is not None
    assert _wrist_height(make_frame(None), "right") is None


def test_a_single_bad_frame_emits_nothing():
    """MediaPipe drops hands constantly. A per-frame status would strobe the panel."""
    debouncer = TrackingDebouncer(hold_frames=3)
    for _ in range(5):
        debouncer.update(check_tracking(make_frame(1.5)))
    assert debouncer.status == "ok"
    assert debouncer.update(check_tracking(make_frame(None))) is None


def test_a_sustained_change_emits_once():
    debouncer = TrackingDebouncer(hold_frames=3)
    for _ in range(5):
        debouncer.update(check_tracking(make_frame(1.5)))
    emitted = [debouncer.update(check_tracking(make_frame(None))) for _ in range(5)]
    assert emitted == [None, None, "hands_hidden", None, None]


def test_the_first_stable_status_is_emitted():
    debouncer = TrackingDebouncer(hold_frames=2)
    assert debouncer.update(check_tracking(make_frame(1.5))) is None
    assert debouncer.update(check_tracking(make_frame(1.5))) == "ok"


def test_an_empty_frame_reads_as_absent():
    debouncer = TrackingDebouncer(hold_frames=1)
    assert debouncer.update(check_tracking(ABSENT)) == "absent"


def test_a_frame_edge_reads_as_clipped():
    debouncer = TrackingDebouncer(hold_frames=1)
    assert debouncer.update(check_tracking(make_frame(1.5, clipped=True))) == "clipped"


def test_the_raw_run_is_visible_before_the_status_is_emitted():
    """The disarm rule in Task 6 acts on sustained absence, and must be able to
    see it building rather than waiting for the debounced edge."""
    debouncer = TrackingDebouncer(hold_frames=10)
    for _ in range(4):
        debouncer.update(check_tracking(ABSENT))
    assert (debouncer.raw, debouncer.run) == ("absent", 4)
    assert debouncer.status is None  # not yet held long enough to emit


class ScriptedExtractor:
    """Replays a fixed list of RawFrames, ignoring the images handed to it.

    Stands in for HolisticExtractor so the loop can be driven frame by frame with
    no camera, no MediaPipe and no timing.
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.served = 0
        self.closed = False

    def process(self, image):
        raw = self.frames[min(self.served, len(self.frames) - 1)]
        self.served += 1
        return image, raw, None

    def close(self):
        self.closed = True


class FakeRecogniser:
    """Returns a canned Prediction. No torch, no checkpoint, no label map file."""

    dominant = "right"
    threshold = 0.6
    min_frames = 8

    def __init__(self, prediction=None, raises=None):
        self.prediction = prediction or Prediction(
            gloss="hello",
            confidence=0.92,
            top3=[("hello", 0.92), ("friend", 0.04), ("truck", 0.01)],
            encode_ms=11.6,
            forward_ms=21.6,
            usable_frames=30,
            n_frames=31,
        )
        self.raises = raises
        self.calls = 0
        self.label_map = ["hello", "friend", "truck"]

    def classify(self, frames):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.prediction


def gesture_frames(rest_before=10, raised=10, rest_after=10):
    """A rest -> raise -> rest sequence: exactly one deliberate gesture."""
    return (
        [make_frame(1.5)] * rest_before
        + [make_frame(0.2)] * raised
        + [make_frame(1.5)] * rest_after
    )


def run_pipeline(frames, *, recogniser=None, start_active=True, store=None, on_event=None):
    """Drive the loop over a fixed frame script and return the events it emitted.

    max_reopen_attempts=1 keeps this the single-pass helper it always was: the
    scripted source exhausting is "the clip ended", not a camera to keep
    retrying — Task 6's reopen loop would otherwise spin forever behind a
    source_factory that always succeeds.
    """
    events: list[dict] = []
    extractor = ScriptedExtractor(frames)
    pipeline = RecognitionPipeline(
        recogniser=recogniser or FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=len(frames)),
        on_event=on_event or events.append,
        extractor_factory=lambda: extractor,
        store=store,
        warmup_frames=1,
        start_active=start_active,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
    )
    pipeline.run()
    return events, pipeline


def test_a_gesture_produces_the_documented_event_sequence():
    """This is the contract the orchestrator is coded against."""
    events, _ = run_pipeline(gesture_frames())
    kinds = [e["e"] for e in events]

    assert "armed" in kinds
    assert "recording" in kinds
    assert kinds.index("armed") < kinds.index("recording")
    assert kinds.index("recording") < kinds.index("classifying")
    assert kinds.index("classifying") < kinds.index("recognised")
    # The full documented tail: classifying -> result -> armed, in that exact
    # order. kinds.index() alone only pins the first occurrence of each kind
    # and would not catch armed being emitted before the result.
    i = kinds.index("classifying")
    assert kinds[i : i + 3] == ["classifying", "recognised", "armed"]


def test_the_recognised_event_carries_every_documented_field():
    events, _ = run_pipeline(gesture_frames())
    recognised = next(e for e in events if e["e"] == "recognised")
    assert recognised["gloss"] == "hello"
    assert recognised["conf"] == 0.92
    assert recognised["top3"] == [["hello", 0.92], ["friend", 0.04], ["truck", 0.01]]
    assert recognised["n_frames"] == 31
    assert recognised["usable_frames"] == 30
    assert recognised["encode_ms"] == 11.6
    assert recognised["forward_ms"] == 21.6
    # Synthetic frames have pose, one un-clipped hand in every frame, so
    # take_quality reads them as cleanly tracked. Verified against the real
    # take_quality, not assumed.
    assert recognised["take_usable"] is True


def test_top3_is_json_safe():
    """Prediction holds tuples; json.dumps turns them into arrays either way, but
    the event must already be the shape the fixture and the consumer expect."""
    events, _ = run_pipeline(gesture_frames())
    recognised = next(e for e in events if e["e"] == "recognised")
    assert all(isinstance(pair, list) for pair in recognised["top3"])


def test_a_declined_prediction_is_unclear_not_recognised():
    """Rule 10: silence is a valid output, and it is the most common one."""
    declined = Prediction(
        gloss=None,
        confidence=0.35,
        top3=[("religion", 0.35), ("truck", 0.27), ("bedroom", 0.13)],
        encode_ms=1.0,
        forward_ms=2.0,
        usable_frames=20,
        n_frames=21,
    )
    events, _ = run_pipeline(gesture_frames(), recogniser=FakeRecogniser(prediction=declined))
    unclear = next(e for e in events if e["e"] == "unclear")
    assert unclear["conf"] == 0.35
    assert "gloss" not in unclear
    assert not any(e["e"] == "recognised" for e in events)


def test_a_too_short_take_is_unclear_with_a_reason():
    """A brief sign is not a broken device — the panel should say 'didn't catch
    that', not raise an error."""
    recogniser = FakeRecogniser(raises=ClipTooShort("5 frames, need 8"))
    events, _ = run_pipeline(gesture_frames(), recogniser=recogniser)
    unclear = next(e for e in events if e["e"] == "unclear")
    assert unclear["reason"] == "too_short"
    assert unclear["conf"] == 0.0
    assert unclear["top3"] == []


def test_recording_events_count_frames_upward():
    events, _ = run_pipeline(gesture_frames())
    counts = [e["frames"] for e in events if e["e"] == "recording"]
    # Strictly increasing, not merely sorted — a constant sequence satisfies
    # "sorted" too and would hide a frame count that stopped advancing.
    assert all(later > earlier for earlier, later in zip(counts, counts[1:], strict=False))
    assert counts[0] >= 1


def test_nothing_is_classified_while_capture_is_paused():
    recogniser = FakeRecogniser()
    events, _ = run_pipeline(gesture_frames(), recogniser=recogniser, start_active=False)
    assert recogniser.calls == 0
    assert not any(e["e"] in {"armed", "recording", "recognised"} for e in events)


def test_tracking_events_are_emitted_while_paused():
    """Framing feedback must work before anyone presses start — otherwise the
    signer cannot get into frame."""
    events, _ = run_pipeline(gesture_frames(), start_active=False)
    assert any(e["e"] == "tracking" for e in events)


def test_pausing_mid_take_discards_it():
    """POST /capture {"active": false} must disarm, or a half-finished take
    survives the pause and completes when capture resumes."""
    recogniser = FakeRecogniser()
    frames = gesture_frames(rest_before=10, raised=4, rest_after=20)
    events: list[dict] = []
    pipeline = None

    def on_event(event):
        events.append(event)
        if event["e"] == "recording" and event["frames"] == 2:
            pipeline.set_capture(False)
            pipeline.set_capture(True)

    extractor = ScriptedExtractor(frames)
    pipeline = RecognitionPipeline(
        recogniser=recogniser,
        source_factory=lambda: FakeFrameSource(n_frames=len(frames)),
        on_event=on_event,
        extractor_factory=lambda: extractor,
        warmup_frames=1,
        start_active=True,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
    )
    pipeline.run()
    assert recogniser.calls == 0


def test_takes_are_saved_as_unlabelled_training_data(tmp_path):
    """Board-recorded takes join the S7 corpus with no adapter."""
    from islkit.infer import ClipStore

    store = ClipStore(
        root=tmp_path / "live", unlabelled_root=tmp_path / "unlabelled", session="test"
    )
    run_pipeline(gesture_frames(), store=store)
    saved = list((tmp_path / "unlabelled" / "test").glob("take_*.npz"))
    assert len(saved) == 1
    assert saved[0].with_suffix(".json").exists()


def test_the_extractor_is_closed_when_the_loop_ends():
    _, pipeline = run_pipeline(gesture_frames())
    assert pipeline._extractor.closed


def test_a_dead_camera_faults_and_does_not_exit():
    """live_demo raises SystemExit here. A device service must survive it."""
    events: list[dict] = []
    frames = gesture_frames()
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=3),
        on_event=events.append,
        extractor_factory=lambda: ScriptedExtractor(frames),
        warmup_frames=1,
        start_active=True,
        drop_limit=2,
        reopen_backoff=(0.0,),
        max_reopen_attempts=1,
    )
    pipeline.run()
    fault = next(e for e in events if e["e"] == "fault")
    assert fault["code"] == "camera_lost"
    assert "msg" in fault


def test_a_camera_that_never_opens_faults_and_retries():
    attempts = []

    def source_factory():
        attempts.append(1)
        raise CameraUnavailable("camera 2 opens but returns no frames")

    events: list[dict] = []
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=source_factory,
        on_event=events.append,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
        reopen_backoff=(0.0,),
        max_reopen_attempts=3,
    )
    pipeline.run()
    assert len(attempts) == 3  # retried, not exited
    assert events[0]["code"] == "camera_open_failed"


def test_health_reports_not_ok_while_a_fault_is_latched():
    def no_camera():
        raise CameraUnavailable("no camera")

    events: list[dict] = []
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=no_camera,
        on_event=events.append,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
        reopen_backoff=(0.0,),
        max_reopen_attempts=1,
    )
    pipeline.run()
    assert pipeline.health()["ok"] is False


def test_walking_out_mid_take_discards_it_instead_of_classifying_the_absence():
    """AutoTake counts an untracked hand as rest, so an absence looks exactly
    like a return to rest and would CLOSE an open take — classifying a gesture
    that was never finished. The disarm rule has to win that race.

    Without _disarm_if_absent this take closes on the second absent frame
    (rest_to_close is reached) and the model is asked to name it.
    """
    frames = [make_frame(1.5)] * 10 + [make_frame(0.2)] * 4 + [ABSENT] * 10 + [make_frame(1.5)] * 10
    recogniser = FakeRecogniser()
    events, _ = run_pipeline(frames, recogniser=recogniser)

    assert any(e["e"] == "recording" for e in events)  # a take really did open
    assert any(e["e"] == "tracking" and e["status"] == "absent" for e in events)
    assert recogniser.calls == 0  # and it was discarded, not classified


def test_a_model_error_faults_but_keeps_the_take_and_the_loop():
    """One bad take must never kill the process, and the clip is still evidence.

    Constructed directly rather than through run_pipeline(): the helper's source
    always exhausts, so its run always ends with a trailing camera_lost fault and
    health()["ok"] would read False regardless of whether model_error ever
    latched. Capturing health() from inside on_event, at the moment the
    model_error fault fires, is what actually pins the finding.
    """
    recogniser = FakeRecogniser(raises=RuntimeError("head is the wrong width"))
    events: list[dict] = []
    ok_when_faulted = []
    pipeline = None

    def on_event(event):
        events.append(event)
        if event["e"] == "fault" and event["code"] == "model_error":
            ok_when_faulted.append(pipeline.health()["ok"])

    frames = gesture_frames()
    extractor = ScriptedExtractor(frames)
    pipeline = RecognitionPipeline(
        recogniser=recogniser,
        source_factory=lambda: FakeFrameSource(n_frames=len(frames)),
        on_event=on_event,
        extractor_factory=lambda: extractor,
        warmup_frames=1,
        start_active=True,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
    )
    pipeline.run()

    fault = next(e for e in events if e["e"] == "fault")
    assert fault["code"] == "model_error"
    assert "head is the wrong width" in fault["msg"]
    assert ok_when_faulted == [False]
    assert recogniser.calls == 1  # it was tried
    assert not any(e["e"] == "recognised" for e in events)  # and produced nothing


def test_a_full_disk_stops_saving_but_not_recognising(tmp_path, monkeypatch):
    """The eMMC is 9.8 GB and 77% full and this writes an npz per gesture."""
    from islkit.infer import ClipStore

    monkeypatch.setattr(
        "recognition.pipeline.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 10 * 1024 * 1024})(),
    )
    store = ClipStore(
        root=tmp_path / "live", unlabelled_root=tmp_path / "unlabelled", session="test"
    )
    events, pipeline = run_pipeline(gesture_frames(), store=store)

    assert any(e["e"] == "recognised" for e in events)  # still recognising
    assert not list(tmp_path.glob("**/take_*.npz"))  # but not writing
    assert pipeline.health()["disk_free_mb"] == 10


def test_stop_ends_the_loop():
    frames = [make_frame(1.5)] * 100
    pipeline = None

    def on_event(event):
        pipeline.stop()

    extractor = ScriptedExtractor(frames)
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(),  # unbounded
        on_event=on_event,
        extractor_factory=lambda: extractor,
        warmup_frames=1,
        start_active=True,
    )
    pipeline.run()  # must return rather than spin forever
    assert extractor.closed


def test_a_model_error_clears_once_a_later_take_succeeds():
    """A watchdog polls /health; one bad take must not mark the service down
    forever once a later take proves the model still works."""
    recogniser = FakeRecogniser(raises=RuntimeError("head is the wrong width"))
    pipeline = RecognitionPipeline(
        recogniser=recogniser,
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda event: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
    )
    take = [make_frame(1.5)] * 10  # content is irrelevant; classify() is canned

    pipeline._classify(take)
    assert pipeline.health()["ok"] is False

    recogniser.raises = None
    pipeline._classify(take)
    assert pipeline.health()["ok"] is True


def test_attempts_reset_after_a_healthy_reopen():
    """A camera that drops once, reconnects, then drops again must not have its
    second backoff pinned by the first outage's ladder — attempts has to reset
    on a successful open, or a camera that drops once a month stays pinned at
    the 8 s ceiling forever."""
    opens = []

    def source_factory():
        opens.append(1)
        if len(opens) == 1:
            raise CameraUnavailable("first open fails")
        return FakeFrameSource(n_frames=1)

    delays = []
    pipeline = None

    def on_event(event):
        if event["e"] == "fault" and event["code"] == "camera_lost":
            if delays:  # this is the second camera_lost; enough to prove the point
                pipeline.stop()

    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=source_factory,
        on_event=on_event,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
        drop_limit=1,
        reopen_backoff=(1.0, 99.0),
    )
    pipeline._stop.wait = delays.append
    pipeline.run()
    # The failed boot-time open uses backoff[0]. The drop after the healthy
    # reconnect must ALSO use backoff[0], not backoff[1] — proof attempts was
    # reset on the successful open in between, not left to climb the ladder.
    assert delays == [1.0, 1.0]


class RaisingExtractor:
    """process() blows up every time, like a MediaPipe graph that stopped working."""

    def __init__(self):
        self.closed = False

    def process(self, image):
        raise RuntimeError("graph is unwell")

    def close(self):
        self.closed = True


def test_a_failing_extractor_factory_faults_model_error_and_does_not_exit():
    """The extractor is built inside the same protective structure as the
    source: an ImportError from a missing MediaPipe asset or a bad aarch64
    wheel must become a fault and a retry, never an escape from run() — which
    would otherwise leave a service that answers /health forever while never
    recognising anything."""
    events: list[dict] = []

    def bad_extractor_factory():
        raise ImportError("libmediapipe_internal.so not found")

    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=events.append,
        extractor_factory=bad_extractor_factory,
        warmup_frames=1,
        reopen_backoff=(0.0,),
        max_reopen_attempts=1,
    )
    pipeline.run()  # must return, never let the ImportError escape
    fault = next(e for e in events if e["e"] == "fault")
    assert fault["code"] == "model_error"
    assert "libmediapipe_internal" in fault["msg"]
    assert pipeline.health()["ok"] is False


def test_extractor_retry_does_not_eat_the_sources_retry_budget():
    """The extractor and the source open at different times — the extractor
    once up front, the source on every camera loss — so a failing extractor
    must not consume the source's own give-up counter before it ever gets to
    try opening a camera."""
    frames = gesture_frames()
    source_opens = []

    def source_factory():
        source_opens.append(1)
        return FakeFrameSource(n_frames=len(frames))

    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=source_factory,
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor(frames),
        warmup_frames=1,
        start_active=True,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
    )
    pipeline.run()
    assert source_opens == [1]  # the source's single-attempt budget was untouched


def test_pausing_invokes_on_pause_but_resuming_does_not():
    """`RecognitionPipeline` does not know what an EventHub is — it only calls
    the callback it was given, on every pause. serve.py binds this to
    hub.clear_take (Task 6.2)."""
    calls = []
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
        on_pause=lambda: calls.append(1),
    )
    pipeline.set_capture(True)
    assert calls == []
    pipeline.set_capture(False)
    assert calls == [1]
    pipeline.set_capture(False)  # idempotent: still fires, still harmless
    assert calls == [1, 1]


def test_set_capture_return_value_matches_the_calls_own_write():
    """The return value must describe this call's own write, not a second,
    racy read of self._capture — the observable half of the fix that captures
    `in_force` inside the lock instead of re-reading after it releases."""
    calls = []
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
        on_pause=lambda: calls.append(1),
    )
    assert pipeline.set_capture(True) is True
    assert calls == []
    assert pipeline.set_capture(False) is False
    assert calls == [1]
    assert pipeline.set_capture(False) is False  # already paused, still reports False
    assert calls == [1, 1]


def test_pipeline_works_with_no_on_pause_callback():
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
    )
    assert pipeline.set_capture(False) is False  # must not raise


class RaisingCloseSource:
    """A source whose close() itself blows up, like a driver-level failure on
    top of a camera that already stopped delivering frames."""

    def __init__(self, n_frames):
        self._inner = FakeFrameSource(n_frames=n_frames)

    def read(self):
        return self._inner.read()

    def close(self):
        raise OSError("device already gone")

    def describe(self):
        return self._inner.describe()


def test_a_source_close_that_raises_does_not_escape_run():
    """Guards the drop path: close() raising on top of a dead camera must not
    itself become the reason run() exits — the same governing rule violation
    as Critical 1, on a colder path."""
    events: list[dict] = []
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: RaisingCloseSource(n_frames=3),
        on_event=events.append,
        extractor_factory=lambda: ScriptedExtractor(gesture_frames()),
        warmup_frames=1,
        drop_limit=2,
        reopen_backoff=(0.0,),
        max_reopen_attempts=1,
    )
    pipeline.run()  # must return, never let the OSError escape
    fault = next(e for e in events if e["e"] == "fault")
    assert fault["code"] == "camera_lost"


def test_drop_limit_scales_down_at_a_low_fps_instead_of_flooring_at_30():
    """At 3 fps a floor of 30 was 10 s, double section 8's ~5 s budget."""
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        warmup_frames=1,
    )
    for i in range(10):
        pipeline._fps.tick(now=100.0 + i / 3.0)  # ~3 fps
    assert pipeline._current_drop_limit() < 30
    assert pipeline._current_drop_limit() >= 2


def test_disk_free_mb_is_populated_before_any_take_is_saved(tmp_path):
    """Rule: /health must be able to warn about a full disk before any gesture
    has happened, not only after the first take is written."""
    from islkit.infer import ClipStore

    store = ClipStore(
        root=tmp_path / "live", unlabelled_root=tmp_path / "unlabelled", session="test"
    )
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([make_frame(1.5)]),
        store=store,
        warmup_frames=1,
    )
    assert isinstance(pipeline.health()["disk_free_mb"], int)


def test_an_unexpected_exception_faults_camera_lost_and_does_not_propagate():
    """A cv2 read or a MediaPipe process() failure must become a fault and a
    reopen, never an unhandled exception — the worst failure mode this design
    has, since it would leave a service that answers HTTP but never recognises
    anything again."""
    events: list[dict] = []
    pipeline = None

    def on_event(event):
        events.append(event)
        if event["e"] == "fault" and event["code"] == "camera_lost":
            pipeline.stop()

    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=5),
        on_event=on_event,
        extractor_factory=lambda: RaisingExtractor(),
        warmup_frames=1,
        reopen_backoff=(0.0,),
    )
    pipeline.run()  # must return, never let the RuntimeError escape
    fault = next(e for e in events if e["e"] == "fault")
    assert fault["code"] == "camera_lost"
    assert "graph is unwell" in fault["msg"]


class _FixedFps:
    """A stand-in for FpsMeter whose rate can be dictated.

    FpsMeter derives `fps` from wall-clock ticks, so a test that wants to say
    "the board is now running at 3 fps" cannot use the real one without
    sleeping. Only `fps` and `tick` are exercised on this path.
    """

    def __init__(self, fps):
        self.fps = fps

    def tick(self, now=None):
        pass


def _armed_pipeline(fps_at_arming):
    """A pipeline past warmup, with its timing derived at a chosen rate."""
    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([]),
        warmup_frames=1,
        start_active=True,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
    )
    pipeline._fps = _FixedFps(fps_at_arming)
    pipeline._arm_segmenter()
    return pipeline


def test_timing_is_re_derived_when_the_observed_rate_moves():
    """Warmup usually measures the room, not the signer.

    MediaPipe skips both hand models when it sees no hands, so an empty room
    runs at ~11 fps on the board and a signer at ~3. Timing derived at 11 and
    never revisited is sized for a take nobody makes.
    """
    pipeline = _armed_pipeline(11.0)
    fast = pipeline._timing

    pipeline._fps = _FixedFps(3.0)  # someone stepped into frame
    pipeline._retime_if_rate_moved()

    assert pipeline._timing != fast
    assert pipeline._timing.pre_roll < fast.pre_roll
    assert pipeline._timing_fps == 3.0


def test_timing_is_left_alone_while_a_take_is_recording():
    """The thresholds a take opened under must be the ones it closes under."""
    pipeline = _armed_pipeline(11.0)
    before = pipeline._timing
    pipeline._auto.state = "recording"

    pipeline._fps = _FixedFps(3.0)
    pipeline._retime_if_rate_moved()

    assert pipeline._timing is before


def test_a_small_rate_wobble_does_not_re_derive():
    """Re-arming costs the next take its pre-roll, so it needs real drift."""
    pipeline = _armed_pipeline(4.0)
    before = pipeline._timing

    pipeline._fps = _FixedFps(4.4)
    pipeline._retime_if_rate_moved()

    assert pipeline._timing is before


def test_a_pinned_min_frames_is_never_overwritten():
    """--min-frames is a deliberate choice; deriving over it would be silent."""
    recogniser = FakeRecogniser()
    recogniser.min_frames = 8
    pipeline = RecognitionPipeline(
        recogniser=recogniser,
        source_factory=lambda: FakeFrameSource(n_frames=1),
        on_event=lambda e: None,
        extractor_factory=lambda: ScriptedExtractor([]),
        warmup_frames=1,
        start_active=True,
        max_reopen_attempts=1,
        reopen_backoff=(0.0,),
        auto_min_frames=False,
    )
    pipeline._fps = _FixedFps(3.0)
    pipeline._arm_segmenter()
    assert recogniser.min_frames == 8
