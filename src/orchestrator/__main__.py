"""Start the orchestrator. Three processes must be running on demo day; this is
the second. `GET /health` reports whether the first is connected, so one check
covers two of the three.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app import Orchestrator
from .audio import DEFAULT_COMMAND, Player
from .hub import EventHub
from .phrases import PhraseTable, PhraseTableError
from .server import DEFAULT_PORT, make_server
from .upstream import DEFAULT_BASE_URL, OFFLINE_AFTER_S, Upstream

log = logging.getLogger("orchestrator")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address; 0.0.0.0 so the App Lab container can reach it")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream", default=DEFAULT_BASE_URL)
    parser.add_argument("--phrases", type=Path, default=Path("phrases.json"))
    parser.add_argument("--audio-dir", type=Path, default=Path("."))
    parser.add_argument("--player", default=" ".join(DEFAULT_COMMAND),
                        help="playback command; the wav path is appended")
    parser.add_argument("--offline-after", type=float, default=OFFLINE_AFTER_S)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        phrases = PhraseTable.load(args.phrases)
    except PhraseTableError as exc:
        log.error("%s", exc)
        return 2

    hub = EventHub()
    player = Player(phrases, args.audio_dir, command=args.player.split())
    upstream = Upstream(args.upstream, offline_after=args.offline_after)
    app = Orchestrator(phrases, hub, player, upstream)
    upstream.set_callbacks(app.on_recognition_event, app.on_offline, app.on_connect)

    try:
        server = make_server(app, args.host, args.port)
    except OSError as exc:
        # Player's audio thread is already running by this point; a bind
        # failure here (port in use is the live risk on this device) must
        # not leak it.
        log.error("cannot bind %s:%d: %s", args.host, args.port, exc)
        player.close()
        return 2

    upstream.start()
    log.info("orchestrator on %s:%d, upstream %s, %d phrases",
             args.host, args.port, args.upstream, len(phrases))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        upstream.stop()
        server.server_close()
        player.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
