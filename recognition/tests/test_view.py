"""The debug view: the sink's handoff, and the guarantees the capture loop relies on.

Nothing here touches OpenCV, MediaPipe or a camera. `annotate` takes its cv2 as
an argument for exactly that reason — the two properties worth pinning are that
it never raises into the capture loop and never draws on the caller's frame, and
both are testable with a stub.
"""

import threading

import numpy as np

from recognition.pipeline import RecognitionPipeline
from recognition.view import ViewSink, annotate


class FakeCv2:
    """Just enough cv2 for annotate: an encoder and a text drawer."""

    IMWRITE_JPEG_QUALITY = 1
    FONT_HERSHEY_SIMPLEX = 0

    def __init__(self, encode_ok=True):
        self.encode_ok = encode_ok
        self.texts = []

    def imencode(self, ext, canvas, params):
        if not self.encode_ok:
            return False, None
        return True, np.frombuffer(b"\xff\xd8jpeg", dtype=np.uint8)

    def putText(self, canvas, text, org, font, scale, colour, thickness):  # noqa: N802
        self.texts.append(text)


class FakeExtractor:
    """Draws by filling the canvas, so "did it draw on a copy?" is observable."""

    def __init__(self, raises=None):
        self.raises = raises
        self.drawn = 0

    def draw(self, frame, results):
        if self.raises is not None:
            raise self.raises
        self.drawn += 1
        frame[:] = 255
        return frame


def a_frame():
    return np.zeros((4, 4, 3), dtype=np.uint8)


OVERLAY = {"tracking": "ok", "take_state": "armed", "capture": True, "fps": 2.15}


# ---------------------------------------------------------------------------
# ViewSink
# ---------------------------------------------------------------------------


def test_nobody_is_watching_a_fresh_sink():
    """The pipeline's gate. False here means it never copies or encodes."""
    assert ViewSink().watching is False


def test_attaching_a_viewer_opens_the_gate():
    sink = ViewSink()
    sink.attach()
    assert sink.watching is True
    assert sink.viewers == 1


def test_the_gate_closes_again_when_the_last_viewer_leaves():
    sink = ViewSink()
    sink.attach()
    sink.attach()
    sink.detach()
    assert sink.watching is True  # one still there
    sink.detach()
    assert sink.watching is False


def test_detaching_more_than_attaching_cannot_go_negative():
    """A handler whose finally runs twice must not make `watching` lie."""
    sink = ViewSink()
    sink.detach()
    sink.detach()
    assert sink.viewers == 0
    assert sink.watching is False


def test_a_published_frame_is_handed_to_a_waiter():
    sink = ViewSink()
    sink.publish(b"first")
    seq, jpeg = sink.wait_for(0, timeout=0.1)
    assert jpeg == b"first"
    assert seq == 1


def test_waiting_on_the_frame_you_already_have_times_out():
    """This timeout is what lets a handler notice a client that went away."""
    sink = ViewSink()
    sink.publish(b"first")
    seq, _ = sink.wait_for(0, timeout=0.1)
    again_seq, jpeg = sink.wait_for(seq, timeout=0.05)
    assert jpeg is None
    assert again_seq == seq


def test_only_the_newest_frame_survives():
    """One slot, not a queue: a slow viewer skips ahead, never replays a backlog."""
    sink = ViewSink()
    for payload in (b"a", b"b", b"c"):
        sink.publish(payload)
    _, jpeg = sink.wait_for(0, timeout=0.1)
    assert jpeg == b"c"
    assert sink.published == 3


def test_a_waiter_is_woken_by_a_publish_from_another_thread():
    sink = ViewSink()
    got = []

    def wait():
        got.append(sink.wait_for(0, timeout=2.0))

    waiter = threading.Thread(target=wait)
    waiter.start()
    sink.publish(b"late")
    waiter.join(timeout=3)

    assert got and got[0][1] == b"late"


# ---------------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------------


def test_annotate_returns_encoded_bytes():
    cv2 = FakeCv2()
    jpeg = annotate(cv2, FakeExtractor(), a_frame(), None, OVERLAY)
    assert jpeg == b"\xff\xd8jpeg"


def test_annotate_draws_on_a_copy_not_the_callers_frame():
    """The frame comes straight off the camera source, which may reuse its
    buffer. Drawing into it would corrupt what the capture loop does next."""
    frame = a_frame()
    annotate(FakeCv2(), FakeExtractor(), frame, None, OVERLAY)
    assert frame.max() == 0, "annotate mutated the caller's frame"


def test_annotate_puts_the_tracking_status_on_the_picture():
    """`clipped` explains a take that never fired, and is invisible in landmarks."""
    cv2 = FakeCv2()
    annotate(cv2, FakeExtractor(), a_frame(), None, dict(OVERLAY, tracking="clipped"))
    assert any("clipped" in text for text in cv2.texts)


def test_annotate_survives_a_missing_fps():
    """fps is None until two frames have arrived; the overlay must not divide by it."""
    cv2 = FakeCv2()
    jpeg = annotate(cv2, FakeExtractor(), a_frame(), None, dict(OVERLAY, fps=None))
    assert jpeg is not None
    assert any("fps" in text for text in cv2.texts)


def test_a_failed_encode_is_a_skipped_frame_not_an_error():
    assert annotate(FakeCv2(encode_ok=False), FakeExtractor(), a_frame(), None, OVERLAY) is None


def test_a_raising_extractor_never_escapes_into_the_capture_loop():
    """The load-bearing property: a debug view must not be able to stop capture."""
    extractor = FakeExtractor(raises=RuntimeError("mediapipe went sideways"))
    assert annotate(FakeCv2(), extractor, a_frame(), None, OVERLAY) is None


# ---------------------------------------------------------------------------
# The pipeline's gate
# ---------------------------------------------------------------------------


def a_pipeline(view=None):
    """A pipeline that is never run — only `_publish_view` is exercised."""
    from test_pipeline import FakeRecogniser

    pipeline = RecognitionPipeline(
        recogniser=FakeRecogniser(),
        source_factory=lambda: None,
        on_event=lambda event: None,
        view=view,
    )
    pipeline._extractor = FakeExtractor()
    pipeline._cv2 = FakeCv2()
    return pipeline


def test_no_view_configured_publishes_nothing():
    a_pipeline(view=None)._publish_view(a_frame(), None)  # must not raise


def test_an_unwatched_view_is_never_fed():
    """Unwatched is the normal case on a demo board, and must cost nothing."""
    sink = ViewSink()
    pipeline = a_pipeline(view=sink)
    pipeline._publish_view(a_frame(), None)
    assert sink.published == 0
    assert pipeline._extractor.drawn == 0


def test_a_watched_view_receives_the_frame():
    sink = ViewSink()
    sink.attach()
    pipeline = a_pipeline(view=sink)
    pipeline._publish_view(a_frame(), None)
    assert sink.published == 1
    assert pipeline._extractor.drawn == 1
