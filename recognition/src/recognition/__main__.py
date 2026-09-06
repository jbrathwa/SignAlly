"""The recognition service: camera in, glosses out over HTTP.

Run:  python -m recognition
      python -m recognition --camera 2 --start-active

Three routes on 127.0.0.1:9978 — GET /results (SSE), POST /capture, GET /health.
The orchestrator is the only consumer. Nothing here knows what a screen is.

The head is still the pretrained 262-class one, so the glosses this streams
**will usually be wrong on your own signing, and that is expected** — S7 replaces
the head. Never read them as an accuracy signal.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import threading
from pathlib import Path

from islkit.infer import ClipStore, SignRecogniser
from recognition.pipeline import CameraSource, RecognitionPipeline
from recognition.server import DEFAULT_PORT, EventHub, make_server
from recognition.view import ViewSink, make_view_server

# Not islkit.plotting.RUNS_DIR: a production service must not derive its
# default checkpoint path from a training workspace's runs/ directory.
DEFAULT_CLASSIFIER = Path(
    os.environ.get("RECOGNITION_CLASSIFIER", "~/models/classifier_262.pt")
).expanduser()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--classifier", type=Path, default=DEFAULT_CLASSIFIER)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--resolution", default="1280x720")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument(
        "--min-frames",
        type=int,
        default=8,
        help="resampling floor, not a duration. encode_clip resamples rather "
        "than pads, so a shorter clip would classify without complaint. At a "
        "low board frame rate a short sign may never reach 8 — re-derive this "
        "against the board's measured rate rather than guessing.",
    )
    ap.add_argument("--root", type=Path, default=Path("data/live"))
    ap.add_argument("--unlabelled-root", type=Path, default=Path("data/live_unlabelled"))
    ap.add_argument("--session", default=None)
    ap.add_argument(
        "--start-active",
        action="store_true",
        help="begin capturing immediately. Off by default: the orchestrator maps "
        "a recognised gloss to speech unconditionally, so capturing before "
        "anyone pressed start would make the device talk unbidden.",
    )
    ap.add_argument("--model-complexity", type=int, default=1, choices=[0, 1, 2])
    ap.add_argument(
        "--view-port",
        type=int,
        default=0,
        help="serve the annotated debug view on this port (0 = off). "
        "Separate from --port on purpose: the API stays on loopback, this does not.",
    )
    ap.add_argument(
        "--view-host",
        default="0.0.0.0",
        help="bind address for --view-port. Anyone who can reach it can watch the camera.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.classifier.exists():
        raise SystemExit(
            f"no classifier at {args.classifier}\n"
            "The checkpoint isn't in this repo "
            "— copy it in, then either pass --classifier <path> or set "
            "RECOGNITION_CLASSIFIER to its location."
        )

    recogniser = SignRecogniser(
        args.classifier, threshold=args.threshold, min_frames=args.min_frames
    )
    store = ClipStore(root=args.root, unlabelled_root=args.unlabelled_root, session=args.session)
    hub = EventHub()

    # The sink is cheap and inert until a browser attaches, so it is built
    # whether or not the view is served — that keeps the pipeline's wiring
    # identical in both cases.
    sink = ViewSink() if args.view_port else None

    def extractor_factory():
        from islkit.infer import HolisticExtractor

        return HolisticExtractor(model_complexity=args.model_complexity)

    pipeline = RecognitionPipeline(
        recogniser=recogniser,
        source_factory=lambda: CameraSource(args.camera, args.resolution),
        on_event=hub.publish,
        extractor_factory=extractor_factory,
        store=store,
        on_pause=hub.clear_take,
        start_active=args.start_active,
        classifier_name=args.classifier.name,
        classifier_sha256=sha256_of(args.classifier),
        view=sink,
    )

    print(f"classifier : {args.classifier}  ({len(recogniser.label_map)} classes)")
    print(f"session    : {store.session}   ->  {args.unlabelled_root}")
    print(f"capture    : {'active' if args.start_active else 'paused'}")
    print(f"listening  : http://{args.host}:{args.port}  /results /capture /health")
    if sink is not None:
        print(
            f"debug view : http://{args.view_host}:{args.view_port}/view"
            "   (camera visible to this network)"
        )
    print()

    threading.Thread(target=pipeline.run, name="capture", daemon=True).start()
    server = make_server(pipeline, hub, args.host, args.port)

    view_server = None
    if sink is not None:
        view_server = make_view_server(sink, args.view_host, args.view_port)
        threading.Thread(target=view_server.serve_forever, name="view", daemon=True).start()

    # systemd and `docker stop` send SIGTERM, not SIGINT. Without this the
    # service is killed rather than closing the camera and the extractor.
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        pipeline.stop()
        server.server_close()
        if view_server is not None:
            view_server.shutdown()
            view_server.server_close()


if __name__ == "__main__":
    main()
