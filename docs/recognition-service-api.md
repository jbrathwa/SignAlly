# Recognition service — install, run, consume

**Camera in, glosses out.** A localhost HTTP service that watches a camera, segments one
deliberate sign at a time, and streams the result. It knows nothing about screens, speech or
vocabulary — that belongs to the orchestrator.

Binds `127.0.0.1:9978`. The orchestrator takes 9977. Needs Python 3.12.

This service now lives at `SignAlly/recognition/`, run as `python -m recognition`. `islkit` — the
landmark encoder, model and inference code — is a separate package it depends on, developed in the
`islkit` project.

---

## 1. Install

Three things must be true before the service starts: Python 3.12, the dependency set, and a
classifier checkpoint **that is not in either repository**.

> ⚠️ **A fresh checkout cannot start the service.** The checkpoint is gitignored upstream and
> was never part of `SignAlly` at all. It lives outside both repos, at `~/models/classifier_262.pt`
> (2.1 MB), with its label map `labels_262.json` beside it in the same directory — the service
> refuses to start if the two disagree about the class count. Regenerating the checkpoint instead of
> copying it means downloading the 3.4 GB training set and retraining. Copy the files.

### Why Python 3.12 exactly

mediapipe 0.10.18 publishes wheels for cp39–cp312 only, and the version is pinned for a hardware
reason that does not go away (section 8). On 3.13 or newer the install simply fails to resolve.
`recognition/pyproject.toml` and `islkit`'s both pin `>=3.12,<3.13`.

### Linux

The straightforward path, and the closest to how the board runs.

```bash
cd SignAlly/recognition
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e /path/to/islkit   # islkit — not on any index, a sibling checkout
pip install -e ".[dev]"                  # the service itself
mkdir -p ~/models && cp /path/to/classifier_262.pt /path/to/labels_262.json ~/models/
.venv/bin/python -m pytest -q            # 94 tests, no camera needed
```

Cameras appear as `/dev/videoN`, and the index passed to `--camera` is that N — except on the UNO Q,
where the index is not stable; see section 8. Your user must be in the `video` group, or opening the device
fails with no frames.

### Windows

Everything installs, but **this has not been run on Windows** — treat the camera step as the risky
one. Use PowerShell.

```powershell
cd SignAlly\recognition
python -m venv .venv
.venv\Scripts\activate
pip install -e \path\to\islkit
pip install -e ".[dev]"
copy \path\to\classifier_262.pt \path\to\labels_262.json %USERPROFILE%\models\
.venv\Scripts\python.exe -m pytest -q
```

Two things differ from Unix, both worth knowing before debugging the wrong thing:

- **The camera backend.** `CameraSource` calls `cv2.VideoCapture(index)` with no backend argument,
  so Windows uses MSMF, which is often slow to open and fails outright on some webcams. If the
  camera never delivers a frame, the one-line local fix is `cv2.VideoCapture(index, cv2.CAP_DSHOW)`
  in `recognition/src/recognition/pipeline.py`. There is no flag for it yet.
- **Shutdown.** `Ctrl+C` stops the service cleanly. The `SIGTERM` handler is a no-op on Windows, so
  a `taskkill` will not close the camera politely.

### macOS

As Linux. The terminal you launch from needs camera permission — macOS asks once; if that prompt is
dismissed the camera silently returns no frames forever after.

### Note on `uv`

`uv sync` works fine inside the `islkit` project, where `islkit` **is** the project. It does
**not** work inside `SignAlly/recognition`: that project's dependency on `islkit` is an editable
install of a sibling checkout, not a package on any index, so `uv run` tries to resolve it from
PyPI and fails with an unsatisfiable-version error. Use the venv's own interpreter directly —
`.venv/bin/python -m recognition`, `.venv/bin/python -m pytest` — for every command in this doc that
runs inside `recognition/`.

---

## 2. Commands, without `make`

`SignAlly/recognition` has no Makefile — it is a small, deliberately plain package. Call its venv's
interpreter directly, everywhere, including on the board.

| What you want | Command |
|---|---|
| Install everything | `pip install -e /path/to/islkit && pip install -e ".[dev]"` |
| Run the service | `.venv/bin/python -m recognition` |
| Run it with flags | `.venv/bin/python -m recognition --camera 2` |
| See every flag | `.venv/bin/python -m recognition --help` |
| Run the tests | `.venv/bin/python -m pytest -q` |

With the venv activated, drop the `.venv/bin/` prefix: `python -m recognition --camera 2`. On
Windows, `.venv\Scripts\python.exe -m recognition`.

`islkit` itself keeps its own `make` targets and a working `uv sync`/`uv run` — those apply inside
the `islkit` project (finding a working camera with `experiments/live_demo.py
--list-cameras`, linting, confirming torch's device, training). None of that changes here; it is
simply a different project from the one this document is about.

---

## 3. Start it

One process, one camera, three routes. It holds the camera exclusively, so nothing else can be using
it — quit Photo Booth, Zoom, and any browser tab with camera permission first.

**1. Check a camera actually delivers frames.** Opening a device succeeds on hardware that never
returns a pixel, so probe it properly. `live_demo.py` stays in the `islkit` project and its
`uv run` works fine there:

```bash
cd /path/to/islkit && uv run python experiments/live_demo.py --list-cameras
```

On the UNO Q, don't do this by hand at all — `scripts/signally-start.sh` probes for a node that
actually delivers a frame automatically; see section 8.

**2. Start the service.** It comes up **paused** — deliberately, see section 5. Run from
`SignAlly/recognition`:

```bash
.venv/bin/python -m recognition
.venv/bin/python -m recognition --camera 2 --start-active
```

**3. Confirm it is alive and measuring.** Give it a few seconds: `fps` reads `0.0` and `autotake` is
`null` until warmup completes.

```bash
curl -s localhost:9978/health | python3 -m json.tool
```

**4. Watch the stream, then start capture.** Leave the first running in its own terminal.

```bash
curl -sN localhost:9978/results
curl -s -X POST localhost:9978/capture -d '{"active":true}'
```

> ⚠️ **The glosses will be wrong.** The head is still the pretrained 262-class one. On your own
> signing it returns things like `truck` for *hello*, and that is expected rather than a defect —
> S7 replaces the head. Use this to verify plumbing, never as an accuracy signal.

---

## 4. The event stream

Capture is delimited by a return to rest, so the service emits exactly one prediction per deliberate
gesture. There is no sliding window and no per-frame classification.

```
wrist
height
        ·············································  raised
                  ╭──────────────────────╮
                 ╱                        ╲
   ─────────────╯                          ╰──────────  rest
        ·············································
        │        │                        │      │
     armed   recording                classifying │
                                             recognised / unclear
        ├────────┤
         pre-roll: rest frames seeded into the take
```

The pre-roll matters. A take that began the instant the hands moved would contain no rest frames at
all, and a clip like that sits outside every class at once.

### Two state axes, not one

Take state cannot answer *is anyone there*. An untracked hand counts as rest, so a signer who walks
away leaves the segmenter sitting in `armed` at an empty room. `tracking` is the separate answer,
and sustained absence disarms the segmenter.

| `tracking` | take state | Means |
|---|---|---|
| `ok` | `armed` | Ready — sign when you like |
| `ok` | `recording` | Signing now |
| `hands_hidden` | `armed` | You are there, hands not visible |
| `clipped` | any | Move back or centre yourself |
| `absent` | `waiting` | Nobody in frame |

### Seven event types

The key is `e`, never `t` — deliberately, so these can never be confused with display-protocol
messages. The first four are the gesture lifecycle, in order; the last three arrive out of band.

| Event | Emitted when | Carries |
|---|---|---|
| `armed` | Waiting for a sign | — |
| `recording` | A take is open — once per frame | `frames` |
| `classifying` | The take closed, inference running | — |
| `recognised` | The model committed to a gloss | `gloss`, `conf`, `top3`, `n_frames`, `usable_frames`, `encode_ms`, `forward_ms`, `take_usable` |
| `unclear` | Below threshold, or the take was too short | `conf`, `top3`, and on the short path `reason`, `n_frames` |
| `tracking` | Framing changed and held ~0.5 s | `status`, `hands`, `clipped` |
| `fault` | Camera or model problem | `code`, `msg` |

> **`unclear` is the normal case, not an error path.** The device is allowed to say *I don't know*,
> and it should — a confident wrong answer puts words in a Deaf person's mouth. Render it as
> "didn't catch that", never as a failure.

Every event as it appears on the wire:

```json
{"e":"tracking","status":"ok","hands":2,"clipped":false}
{"e":"armed"}
{"e":"recording","frames":14}
{"e":"classifying"}
{"e":"recognised","gloss":"hello","conf":0.92,"top3":[["hello",0.92],["howareyou",0.04],["friend",0.01]],"n_frames":31,"usable_frames":30,"encode_ms":11.6,"forward_ms":21.6,"take_usable":true}
{"e":"unclear","conf":0.35,"top3":[["religion",0.35],["truck",0.27],["bedroom",0.13]]}
{"e":"unclear","conf":0.0,"top3":[],"reason":"too_short","n_frames":5}
{"e":"fault","code":"camera_lost","msg":"camera stopped returning frames after 412 frames"}
```

All of these ship as `recognition/tests/fixtures/recognition-events-v1.jsonl`, so the orchestrator
can be built and tested before this service ever runs.

**Fields worth knowing.** `take_usable` says whether the camera saw the take well enough for the
answer to mean anything — it separates "the model was wrong" from "the camera never saw you", which
is invisible in a gloss alone. `top3` is a diagnostic: log it, never display it. `reason` appears
only on the too-short `unclear`, so treat it and `n_frames` as optional.

**Fault codes — three, and only three.** New failure modes reuse these rather than adding a fourth.

| Code | Cause | What the service does |
|---|---|---|
| `camera_lost` | Frames stopped, or the camera or MediaPipe started raising | Latches unhealthy, disarms, reopens with backoff |
| `camera_open_failed` | The device would not open at all | Retries with backoff, keeps serving |
| `model_error` | The classifier or the extractor raised | Keeps the clip as evidence, continues, clears on the next good take |

None of these exits the process. A camera failure is a fault and a retry — the service stays up and
answerable, because a dead process on a headless board tells you nothing.

---

## 5. The three routes, and the debug view

### `GET /results` — the stream

Server-Sent Events. No `Content-Length`; the body is delimited by the connection closing, which is
what lets it stream indefinitely. At most four concurrent subscribers — the fifth gets `503`.

```
$ curl -sN localhost:9978/results

data: {"e":"tracking","status":"ok","hands":2,"clipped":false}

: ping

data: {"e":"armed"}
```

### `POST /capture` — start and stop

Returns the state now in force. Pausing also discards any take in progress, so a half-finished
gesture cannot survive the pause and complete when capture resumes.

```bash
curl -s -X POST localhost:9978/capture -d '{"active":true}'
# {"capture": true}
```

| Status | When |
|---|---|
| 200 | Accepted — body is `{"capture": true\|false}` |
| 400 | Body is not `{"active": true\|false}`, `active` is not a boolean, or `Content-Length` is not a number |
| 413 | Body over 4096 bytes |

### `GET /health` — one probe, whole picture

`200` with the snapshot, or `500` if the snapshot itself fails. Anything else is `404`.

```json
{"ok": true, "capture": false, "fps": 4.21, "classes": 262,
 "classifier": "classifier_262.pt", "sha256": "26051eeb…",
 "tracking": "ok", "state": "armed", "uptime_s": 812.4, "frames": 3417,
 "source": {"kind": "camera", "index": 2, "width": 640, "height": 480,
            "aspect": 1.3333, "requested": "640x480"},
 "autotake": {"pre_roll": 4, "rest_to_arm": 2, "raised_to_start": 2,
              "rest_to_close": 3, "max_frames": 135},
 "min_frames": 8, "threshold": 0.6, "disk_free_mb": 2140,
 "subscribers": 1, "dropped_events": 0,
 "islkit_version": "0.2.0", "encoder_fingerprint": "6a85d7da43fc785d"}
```

| Field | Meaning |
|---|---|
| `ok` | `false` while a fault is latched. The watchdog's single question |
| `capture` | Whether capture is running right now |
| `fps` | Rolling measured rate. `0.0` until two frames have arrived |
| `state` | Segmenter state: `waiting`, `armed` or `recording` |
| `tracking` | Last framing verdict, or `null` before warmup |
| `autotake` | The frame counts warmup derived from the measured rate |
| `source` | Camera identity and the geometry it actually delivered |
| `subscribers` | Open `/results` connections, of a maximum four |
| `dropped_events` | Events discarded because a subscriber was too slow |
| `disk_free_mb` | Free space where takes are written |
| `islkit_version` | The installed `islkit` package version serving this checkpoint |
| `encoder_fingerprint` | Behavioural hash of what the feature encoder currently does. Recorded in the checkpoint at training time and recomputed at load; a mismatch means the classifier is being served by an encoder it was not fitted on — a silent-and-confident failure mode this project has hit before |

### `GET /view` — the annotated debug view, and why it isn't loopback

A fourth surface, separate from the three API routes above. The service's own flag default is off
(`--view-port 0`), but **`scripts/signally-start.sh` turns it on by default** (`VIEW_PORT=9979`) —
so on the board, unless something has been changed, this is running right now. When enabled it
serves an MJPEG stream of the camera with the extracted landmarks drawn on it — `/`, `/view` and
`/index.html` return an HTML page embedding it, `/view.mjpg` is the raw stream — so a tester can see
what the model sees instead of guessing from a gloss alone.

> ⚠️ **This is the one surface that is not loopback-only, and that is deliberate but worth knowing
> before you rely on the default.** `--port` (the API: `/results`, `/capture`, `/health`) binds
> `127.0.0.1` and nothing off the machine can reach it. `--view-port` binds `--view-host`, which
> **defaults to `0.0.0.0`** — on purpose, so a laptop on the same network can watch the camera while
> the service runs on the board. The consequence is exactly what it sounds like: on the board, right
> now, at the script's defaults, **anyone who can reach the device on the network can watch the
> camera** through `/view`. On a shared or untrusted network, set `VIEW_PORT=0` before starting (or
> pass `--view-port 0` directly) to turn it off entirely, or point `--view-host` at a narrower
> address.

### Why `autotake` changes between machines

The segmenter's thresholds were tuned at 12.5 fps on a laptop. The board is far slower, so the
service measures its real rate at startup and scales the counts. A literal `rest_to_arm` of 6 would
be a two-second wait before the device accepted a sign; a literal `max_frames` of 400 would let
someone who froze mid-sign record for over two minutes.

| Threshold | at 12.5 fps | at 3 fps |
|---|---:|---:|
| `pre_roll` | 12 | 3 |
| `rest_to_arm` | 6 | 2 |
| `raised_to_start` | 6 | 2 |
| `rest_to_close` | 8 | 2 |
| `max_frames` | 400 | 96 |

`min_frames` is the exception — a resampling floor, not a duration, so it does not scale. At a low
enough rate a short sign never reaches it and comes back `unclear` with `reason: "too_short"`. If
that happens often on the board, lower `--min-frames` against the real measured rate rather than
guessing.

---

## 6. Flags

`.venv/bin/python -m recognition --help` (from `SignAlly/recognition`) prints these.

| Flag | Default | Notes |
|---|---|---|
| `--classifier` | `~/models/classifier_262.pt` (or `$RECOGNITION_CLASSIFIER`) | Refuses to start if missing |
| `--camera` | `0` | Device index. **Not stable on the UNO Q — probe, don't hardcode. See section 8** |
| `--resolution` | `1280x720` | Mismatches reported, never enforced — aspect changes the encoded geometry, so takes at different aspects are not comparable |
| `--host` | `127.0.0.1` | Loopback. Nothing outside this machine should reach it |
| `--port` | `9978` | 9977 belongs to the orchestrator |
| `--threshold` | `0.6` | Below this the answer is `unclear`. Not settled — see section 8 |
| `--min-frames` | `8` | A resampling floor, not a duration |
| `--root` | `data/live` | Labelled takes (unused by the service) |
| `--unlabelled-root` | `data/live_unlabelled` | Where recorded takes land |
| `--session` | timestamp | Session id in the take path |
| `--start-active` | off | Capture from boot instead of waiting for the orchestrator |
| `--model-complexity` | `1` | Holistic complexity: 0, 1 or 2. Drop to `0` if the board is too slow |
| `--view-port` | `0` (off) | Serves the annotated debug view (section 5) on this port. Separate from `--port` on purpose: the API stays on loopback, this does not |
| `--view-host` | `0.0.0.0` | Bind address for `--view-port`. Anyone who can reach it can watch the camera — see section 5's warning |

> **Every gesture is recorded.** Each take is written as raw landmarks plus a sidecar under
> `--unlabelled-root`, in the layout `build_dataset` already globs. Board-recorded takes join the S7
> corpus with no adapter — field data for free. Below roughly 500 MB free the service stops writing,
> keeps recognising, and says so in `disk_free_mb`.

---

## 7. Consuming it from the orchestrator

Four things will bite you if not handled deliberately.

1. **Reset the offline timer on *any* line, comments included.** The stream sends `: ping` every
   2 seconds. An idle signer produces no events for minutes, so a liveness timer that only counts
   `data:` lines will flash "Recognition offline" every 5 seconds while someone stands still.
2. **Capture starts paused.** Nothing is recognised until `POST /capture {"active":true}`. This is
   on purpose: a translation table that maps a recognised gloss straight to speech would make the
   device talk before anyone pressed start. Gate on your own device state too.
3. **Ignore unknown fields and unknown event types.** Extra keys are added over time and existing
   ones vary by path — the two `unclear` shapes differ.
4. **Reconnect freely; state arrives immediately.** On connect you are replayed the current framing
   status and the current take state. While paused, only `tracking` is replayed — you issued the
   pause, so you already know the rest.

Killing the orchestrator does not disturb this service. It keeps running, keeps recording takes, and
keeps serving `/health`. Capture state is the service's own, not derived from who is connected.

---

## 8. Setting up the Arduino UNO Q

The board is not a small Linux machine that happens to be slow. Four of its constraints are hard,
and each has already forced a design decision.

> ⚠️ **Run on the Linux host, never in an App Lab container.** MediaPipe cannot execute inside an
> App Lab app on this board — the only version a container's Python installs needs ARMv8.1-A LSE
> atomics, and the Cortex-A53 in the QRB2210 does not have them. It imports and then aborts. This
> service is a plain Python process on the Linux side. The App Lab container is only ever a courier.

### Before installing anything

- **Unplug the host computer.** The USB-C port cannot serve the webcam and a host at the same time —
  connecting a laptop flips the data role and the camera's video node disappears entirely, whatever
  index it happened to be at. Power the board from VIN (7–24 V) so USB-C stays free.
- **Check the disk.** The eMMC is 9.8 GB and was 77% full. Torch is not small and is the thing most
  likely not to fit. Run `df -h /` first.
- **Check the RAM.** 1.7 GB total, roughly 1.2 GB available, and this process loads torch, Holistic
  and a classifier at once.
- **No root.** `sudo` has no password available, so nothing in the runtime may need it. Install into
  a user virtualenv.

### Install a trimmed dependency set

Not the bench's full one — `pandas`, `pyarrow` and `xgboost` are training-time only and waste scarce
disk. `islkit` isn't on any package index, and it is not published, so the board
can't `pip install` or `git clone` it directly. Build a wheel on the laptop and copy that instead —
it needs no credentials on the device.

```bash
# On the laptop, inside islkit:
uv build --wheel                                     # -> dist/islkit-0.2.0-py3-none-any.whl
scp dist/islkit-0.2.0-py3-none-any.whl board:~/

# On the board:
df -h /                                              # before anything: is there room?
python3.12 -m venv ~/SignAlly/recognition/.venv
source ~/SignAlly/recognition/.venv/bin/activate

pip install ~/islkit-0.2.0-py3-none-any.whl           # pulls mediapipe, numpy, torch — no extras
cd ~/SignAlly/recognition && pip install -e . --no-deps   # the service itself; islkit already satisfies it
```

`opencv-python-headless` isn't installed by hand: `mediapipe==0.10.18` hard-requires
`opencv-contrib-python` and installs it regardless of anything requested alongside it, so there is
nothing to choose here — see `islkit/pyproject.toml`'s own comment on this.

> ⚠️ **The pins are not negotiable.** **mediapipe 0.10.18 exactly** — the last release with an
> aarch64 wheel that runs on ARMv8.0-A. There is no upgrade path on this hardware, ever; the CPU is
> the blocker, not the software. **Python ≤3.12**, because 0.10.18 builds only for cp39–cp312. And
> **numpy below 2**, which MediaPipe drags behind it — upgrading numpy uninstalls the landmark
> extractor.

### Copy the model across

Not in git, and not inside either repo any more. Both files must land in the same directory.

```bash
scp classifier_262.pt labels_262.json  board:~/models/
```

### Find the camera

**Don't hardcode an index.** It moved from `video1` to `video2` across a reboot on 2026-09-06, and
the two Venus hardware codec nodes take whichever indices the camera itself isn't using — so a fixed
number that worked yesterday can silently point at a codec today. Opening a codec node succeeds
exactly like opening a real camera; it just never returns a frame, which reads as a hung camera
rather than a wrong index and has already cost hours here.

Probe for the node that actually delivers a frame instead — `scripts/signally-start.sh` already does
this (`detect_camera` opens each `/dev/videoN` in turn and keeps the first one that reads a real
frame) and passes `--camera auto` through as the number it found:

```bash
.venv/bin/python -m recognition --camera <the index detect_camera found> --resolution 640x480
```

`scripts/signally-start.sh start` is the normal path — it runs `detect_camera` and passes the result
through for you. Invoke `python -m recognition --camera` by hand only while debugging.

### Measure the frame rate before trusting anything

This is the gate. Even though the stack has since run on this board — `signally-start.sh`'s own
history of the camera moving from `video1` to `video2` is evidence Holistic has executed here and
captured frames — no sustained fps figure for the current checkpoint, resolution and
`--model-complexity` has been written down anywhere. The ~5 fps figure in the original architecture plan is
MediaPipe **Hands** — this runs pose plus a 478-point refined face mesh plus both hands, which is far
heavier — so that number still does not transfer. Read the real one from `/health` on every
deployment rather than assuming a figure from a different run.

```bash
curl -s localhost:9978/health | python3 -m json.tool | grep -E 'fps|autotake' -A6
```

Read `fps` after a minute, then check `autotake` and `min_frames` against it. If takes keep returning
`reason: "too_short"`, lower `--min-frames`. If the rate is dire, try `--model-complexity 0` before
changing anything structural.

> **Do not add the mirror.** the original architecture plan section 2.2 shows `cv2.flip(frame, 1)` in its vision
> pipeline. That belonged to MediaPipe *Hands*, whose handedness classifier assumes a
> selfie-mirrored image. This service never reads that label — it assigns hand slots geometrically
> (rule 3) — so the flip corrects nothing and corrupts the geometry the encoder depends on. Measured
> through the real checkpoint: **100.0% unmirrored, 28.3% mirrored.**

### Three processes on demo day

This service, the orchestrator, and the App Lab courier. Treat "are all three running?" as something
verified, never assumed — the orchestrator's `/health` is meant to report whether recognition is
connected, so one check covers two of the three, and the courier's liveness shows up as ACKs
arriving.

---

## 9. When it misbehaves

| Symptom | Likely cause and fix |
|---|---|
| Startup fails naming the classifier | The checkpoint is not there — it lives outside both repos. Copy `classifier_262.pt` and `labels_262.json` into `~/models/`, or point `--classifier`/`RECOGNITION_CLASSIFIER` at wherever they landed |
| Install cannot resolve mediapipe | Python is newer than 3.12. 0.10.18 has no wheel for it |
| Class-count mismatch on startup | `labels_262.json` and the checkpoint are from different runs. They ship as a pair |
| `fault camera_open_failed`, retrying | Another process holds the camera, the index is wrong, or on Linux your user is not in the `video` group. On the UNO Q, check a laptop is not plugged into USB-C |
| Camera never opens on Windows | The MSMF backend. Try `cv2.CAP_DSHOW` in `CameraSource` |
| Stream shows only `: ping` | Capture is paused. Check `capture` in `/health` and post `{"active":true}` |
| `tracking: absent` with someone in frame | MediaPipe cannot find a pose. Usually lighting, or framed too tight to see shoulders |
| `tracking: clipped` | Shoulders or hands at the frame edge, so the geometry is truncated rather than missing. Move back |
| Never leaves `armed` while signing | The raise is not being detected. Begin and end each take with hands resting down — rest is the delimiter |
| Every take comes back `too_short` | `fps` is too low for `min_frames`. Check both in `/health`, then lower `--min-frames` |
| `503` on `/results` | Four subscribers already. A closed connection frees its slot within about one heartbeat, not instantly |
| `dropped_events` climbing | A consumer is reading slower than the stream produces. The service drops the oldest rather than stalling |
| Takes stop appearing on disk | Under 500 MB free. Recognition continues; check `disk_free_mb` |

---

## 10. Known limits

> **This has now run against real camera hardware — the claim used to be otherwise.** This section
> once said `CameraSource` had never executed against a physical device. That is no longer true: this
> stack runs on the UNO Q today, launched by `scripts/signally-start.sh`, and the script's own
> comments are the evidence — its camera-index history (moved from `video1` to `video2` across a
> 2026-09-06 reboot) could only have been observed by opening real devices on real hardware and
> reading real frames. Torch, mediapipe and the camera have all executed on the board; that specific
> unknown is resolved.
>
> What is genuinely still open: **no sustained, written-down fps figure** exists for a given
> checkpoint/resolution/`--model-complexity` combination — read it fresh from `/health` rather than
> trusting a number quoted here or elsewhere (section 8). The **Windows** path has still never been run at
> all — the camera step there stays the risky one. And the two limits below remain unsettled.

- **The confidence threshold is not settled.** 0.6 is a placeholder — the model is wildly
  overconfident out of distribution (0.9978 on random noise), so a plain softmax cutoff cannot
  separate "confident and right" from "confident and garbage". Re-derive against the fine-tuned head.
- **The vocabulary is not reconciled.** `data/vocab.json` holds 17 glosses; the original architecture plan is
  written around about twelve phrases. The orchestrator's phrase table is keyed by gloss, so a
  mismatch means a recognised sign the device cannot say. Settle before writing that table.
