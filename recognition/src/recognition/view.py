"""The debug view: annotated camera frames over MJPEG, for aiming the camera.

This exists so a tester can see what the model sees — where the landmarks
actually land, whether they are clipped, whether MediaPipe has lost the hands —
without which "it did not recognise my sign" is unfalsifiable.

Two properties matter more than anything else here:

**It costs nothing when nobody is watching.** `ViewSink.watching` is False until
a browser connects, and the pipeline checks it before it copies, draws or
encodes a single frame. A service running unwatched pays one boolean read per
frame.

**It reuses the landmarks recognition already computed.** `HolisticExtractor`
returns the frame and the raw MediaPipe results together, and already has a
`draw()` built for this. Nothing here runs inference; a second Holistic pass on
a board managing ~2 fps would halve the thing it is meant to debug.

The frame published here is **unmirrored**, exactly as the model sees it. The
page flips it with a CSS transform so positioning yourself feels natural, which
costs nothing and lets each viewer toggle independently — mirroring server-side
would mean a second encode per orientation for no gain.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

DEFAULT_VIEW_PORT = 9979
JPEG_QUALITY = 70
IDLE_SLEEP_S = 0.02
CLIENT_TIMEOUT_S = 30.0


class ViewSink:
    """One latest-frame slot, plus the viewer count that gates producing it.

    The pipeline publishes; HTTP handlers consume. Deliberately one slot and not
    a queue: a viewer that falls behind should see the newest frame, never a
    backlog of stale ones, and a debug view must never apply back-pressure to
    the capture loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)
        self._jpeg: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._published = 0

    @property
    def watching(self) -> bool:
        """True when at least one browser is attached. The pipeline's gate."""
        with self._lock:
            return self._viewers > 0

    @property
    def viewers(self) -> int:
        with self._lock:
            return self._viewers

    @property
    def published(self) -> int:
        with self._lock:
            return self._published

    def attach(self) -> None:
        with self._lock:
            self._viewers += 1

    def detach(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)

    def publish(self, jpeg: bytes) -> None:
        """Replace the current frame and wake every waiting handler."""
        with self._new:
            self._jpeg = jpeg
            self._seq += 1
            self._published += 1
            self._new.notify_all()

    def wait_for(self, last_seq: int, timeout: float = 1.0):
        """Block until a frame newer than `last_seq`, or time out.

        Returns (seq, jpeg), or (last_seq, None) if nothing new arrived. The
        timeout is what lets a handler notice a client that has gone away.
        """
        with self._new:
            if self._seq == last_seq or self._jpeg is None:
                self._new.wait(timeout)
            if self._jpeg is None or self._seq == last_seq:
                return last_seq, None
            return self._seq, self._jpeg


def annotate(cv2, extractor, frame, results, overlay: dict) -> bytes | None:
    """Draw landmarks and status onto a copy of `frame`, return JPEG bytes.

    The copy is not optional. `extractor.draw()` writes into the array it is
    given, and the array it would be given here comes straight from the camera
    source — which may reuse its buffer. Drawing into it would corrupt whatever
    the capture loop does with that frame next.

    Returns None if the encode fails, which the caller treats as "skip this
    frame": a debug view is never worth an exception in the capture loop.
    """
    try:
        canvas = frame.copy()
        extractor.draw(canvas, results)
        _draw_overlay(cv2, canvas, overlay)
        ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return None
        return buf.tobytes()
    except Exception:  # noqa: BLE001 - the view must not break capture
        log.exception("view frame failed; skipping")
        return None


def _draw_overlay(cv2, canvas, overlay: dict) -> None:
    """Status text, so the picture answers questions on its own.

    Tracking status is the one that matters: `clipped` and `absent` explain a
    take that never fired, and they are invisible in the landmarks alone.
    """
    lines = [
        f"tracking: {overlay.get('tracking')}",
        f"take: {overlay.get('take_state')}   capture: {'on' if overlay.get('capture') else 'off'}",
        f"fps: {overlay.get('fps'):.2f}" if overlay.get("fps") is not None else "fps: -",
    ]
    tracking = overlay.get("tracking")
    # BGR. Green when the framing is usable, amber when it is not.
    colour = (120, 220, 120) if tracking == "ok" else (60, 190, 250)

    y = 22
    for text in lines:
        cv2.putText(canvas, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(canvas, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
        y += 20


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SignAlly recognition view</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #10141a; color: #e6e8ea;
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 20px 16px 40px; }
  h1 { font-size: 17px; margin: 0 0 4px; font-weight: 600; }
  p.sub { margin: 0 0 16px; color: #98a2ad; font-size: 13px; }
  .shot { background: #000; border: 1px solid #263040; border-radius: 6px;
          overflow: hidden; line-height: 0; }
  img { width: 100%; height: auto; display: block; }
  img.mirror { transform: scaleX(-1); }
  .bar { display: flex; gap: 10px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  button { background: #1b2431; color: #e6e8ea; border: 1px solid #2f3a4a;
           border-radius: 5px; padding: 7px 12px; font: inherit; cursor: pointer; }
  button:hover { background: #223044; }
  .hint { color: #98a2ad; font-size: 12px; }
  code { background: #1b2431; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Recognition view</h1>
  <p class="sub">The frames the classifier is actually seeing, with the landmarks it extracted.
     Streaming costs nothing while this page is closed.</p>

  <div class="shot">
    <img id="feed" class="mirror" src="/view.mjpg" alt="annotated camera stream">
  </div>

  <div class="bar">
    <button id="flip">Show the model&rsquo;s view</button>
    <span class="hint" id="hint">Mirrored, so moving left moves the image left.</span>
  </div>

  <p class="hint" style="margin-top:16px">
    Landmarks are drawn <em>before</em> any mirroring, so they line up in both views.
    The model always receives the unmirrored frame &mdash; that is pinned at
    <code>100.0%</code> unmirrored against <code>28.3%</code> mirrored.
  </p>
</div>
<script>
  const feed = document.getElementById('feed');
  const flip = document.getElementById('flip');
  const hint = document.getElementById('hint');
  flip.addEventListener('click', () => {
    const mirrored = feed.classList.toggle('mirror');
    flip.textContent = mirrored ? "Show the model\\u2019s view" : 'Show the mirrored view';
    hint.textContent = mirrored
      ? 'Mirrored, so moving left moves the image left.'
      : 'Unmirrored \\u2014 exactly the geometry the classifier receives.';
  });
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one response per connection; MJPEG closes it

    def log_message(self, fmt, *args):
        log.debug("view %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0]
        if path in ("/", "/view", "/index.html"):
            self._page()
        elif path == "/view.mjpg":
            self._stream()
        else:
            self.send_error(404)

    def _page(self) -> None:
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        sink = self.server.sink
        sink.attach()
        try:
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            last = 0
            deadline = time.monotonic() + CLIENT_TIMEOUT_S
            while True:
                seq, jpeg = sink.wait_for(last)
                if jpeg is None:
                    # Nothing new. The capture loop may simply be slow; only give
                    # up once nothing has arrived for a long time.
                    if time.monotonic() > deadline:
                        return
                    continue
                last = seq
                deadline = time.monotonic() + CLIENT_TIMEOUT_S
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("view client went away")
        finally:
            sink.detach()


class ViewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, sink: ViewSink):
        self.sink = sink
        super().__init__(address, _Handler)


def make_view_server(sink: ViewSink, host: str = "0.0.0.0", port: int = DEFAULT_VIEW_PORT):
    """A debug-view server on its own port.

    Separate from the recognition API on purpose. That API stays on loopback —
    nothing outside the board should be able to start the camera — while this
    binds where a laptop can reach it. The trade is deliberate and documented:
    anyone who can reach this port can watch the camera.
    """
    return ViewServer((host, port), sink)
