# Orchestrator

The device's brain stem. Consumes the recognition service's event stream and is
the only component that speaks the display JSON protocol v1.

## Run it

Standard library only — no venv needed, including on the board's Python 3.13.
The package is not installed, so `src` has to be on the path, and `--phrases`
and `--audio-dir` default to paths relative to the working directory — run
these from the repo root:

```bash
PYTHONPATH=src python3 -m orchestrator       # 0.0.0.0:9977, upstream 127.0.0.1:9978
PYTHONPATH=src python3 -m orchestrator --log-level DEBUG
```

## The API

| Route | Who calls it | Body |
|---|---|---|
| `GET /ping` | courier, probing for the host | `{"ok":true}` |
| `GET /events` | courier | SSE; each `data:` is one protocol JSON object |
| `POST /uplink` | courier | one protocol JSON object — the panel's `button`, `hello`, `ack` |
| `GET /health` | you | whether recognition is connected, and everything else |

It binds `0.0.0.0` deliberately: the App Lab container reaches it across the
docker gateway, so loopback-only would break the display chain.

## Tests

```bash
python -m pip install -e ".[dev]"
python -m pytest -v
```

Everything runs with no board, no camera and no sound, against the recorded
fixtures in `tests/fixtures/` and an in-process fake recognition server.

## Known limits

- **It makes no sound on the UNO Q.** The board has no reachable audio output. The path is built and tested; the
  wav files do not exist yet. Missing files are logged and skipped.
- **Two definition-of-done items are unverified** — `/ping` answering from
  inside an App Lab container, and `stop` halting capture in the *live*
  recognition service. Both need the courier rewrite, which is a follow-up.
- **The English phrase strings are a first draft** and need review by whoever
  owns the ISL vocabulary. `you`, `friend`, `mother` and `alright` are the
  doubtful ones.
- **`tracking: ok` re-emitting state** is an orchestrator-side recovery the
  protocol spec does not describe. It uses only v1 messages, but the panel has
  never been tested against it.
- **It is unauthenticated and binds `0.0.0.0`.** Anyone on the same LAN can
  `POST /uplink` to start or stop the camera and mute the device, and can `GET
  /events` to read a live transcript of what a Deaf user is signing. The bind
  is required — the App Lab container reaches it across the docker gateway —
  and for a hackathon device on a trusted network this is an accepted risk, not
  an oversight. Off that network, bind the docker bridge address instead of
  `0.0.0.0`, or put a shared-secret header on `/uplink`; the header needs a
  matching change in the courier, which does not exist yet.
