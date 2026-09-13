#!/usr/bin/env bash
#
# Start, stop and inspect the whole SignAlly stack on the UNO Q.
#
# Three processes, and the point of this script is that "are all three running?"
# becomes something you verify rather than assume:
#
#   1. recognition   host, SignAlly/recognition/.venv (Python 3.12), holds the camera
#   2. orchestrator  host, system Python 3.13, no venv (it is stdlib-only)
#   3. console       App Lab container, stands in for the display panel
#
# Everything here is deliberately paranoid about two things this board has
# already cost us hours on:
#
#   * `pkill -f serve.py` matches its own SSH command line and kills the shell
#     that ran it. The bracket trick alone was not enough either, so every stop
#     is CONFIRMED BY PORT, never by an exit code.
#   * Only one process can hold the camera. Starting recognition twice gives you
#     a confusing camera_open_failed rather than an obvious "already running".
#
# Usage: signally-start.sh {start|stop|restart|status|logs [name]}

set -uo pipefail

RECOGNITION_DIR="${RECOGNITION_DIR:-$HOME/SignAlly/recognition}"
RECOGNITION_PY="${RECOGNITION_PY:-$HOME/SignAlly/recognition/.venv/bin/python}"
SIGNALLY_DIR="${SIGNALLY_DIR:-$HOME/SignAlly}"
ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-$HOME/SignAlly/orchestrator}"
PHRASES="${PHRASES:-$SIGNALLY_DIR/phrases.json}"
CONSOLE_APP="${CONSOLE_APP:-$HOME/SignAlly/applab/signally-console}"
LOG_DIR="${LOG_DIR:-$HOME/logs}"

# Outside the repo on purpose. Takes are training data, and the production
# repo should not silently accumulate them — copying them into a training
# workspace stays an explicit step.
TAKES_DIR="${TAKES_DIR:-$HOME/takes}"

# The camera index is NOT stable. Across a reboot on 2026-09-06 the working
# node moved from video1 to video2, and the Venus hardware codecs took the
# indices the camera had. Opening a codec looks like a camera that returns no
# frames, which is a confusing way to lose an hour. So: probe, never assume.
# Override with CAMERA=n to skip the probe.
CAMERA="${CAMERA:-auto}"
RESOLUTION="${RESOLUTION:-640x480}"

# XNNPACK threads per MediaPipe inference node. MediaPipe ships every node with
# an empty `xnnpack {}` block, which means ONE thread — measured here as 0.99 of
# this board's four cores and ~2.2 fps with a signer in shot. That is under the
# rate at which recognition stops working at all (2.5% top-1 at 2 fps against
# 100% at 5), so this is not a tuning knob, it is the difference between a
# device that recognises signs and one that returns confident noise.
# Measured on 2026-09-07 against a signer, replaying one captured burst through
# each setting so the scene could not drift between them:
#
#     threads   1      2      3      4
#     fps       2.32   4.01   5.38   4.07
#     cores     1.08   2.04   2.92   3.67
#
# 3 wins and clears 5 fps. 4 oversubscribes the board's four cores and hands
# most of the gain back, so MORE IS NOT BETTER — re-measure before changing it.
# Landmark output is bit-identical to the unthreaded path (max abs diff 0.0).
# MP_THREADS=0 restores MediaPipe's own default.
MP_THREADS="${MP_THREADS:-3}"

# The 6-sign head fine-tuned on the signer's own recordings (islkit's
# experiments/finetune.py): hello, thankyou, washroom, doctor, happy, sad.
# labels_6.json must sit beside it. The 17-class INCLUDE-only head scored 2/24
# on this signer, so it is not the default any more.
CLASSIFIER="${CLASSIFIER:-$HOME/models/classifier_6.pt}"

# The annotated debug view, on its own port. The recognition API stays on
# loopback — nothing off the board should be able to start the camera — but this
# binds to 0.0.0.0 so a laptop can watch. Anyone who can reach it can see the
# camera; set VIEW_PORT=0 to turn it off entirely.
VIEW_PORT="${VIEW_PORT:-9979}"

REC_PORT=9978
ORC_PORT=9977
WAIT_SECONDS=45

# MediaPipe writes a wall of noise to stderr on every start.
NOISE='^W0|absl|xnnpack|cpuinfo|feedback|landmark_projection|Fiber init|TensorFlow Lite'

# hostname -I lists the docker bridge addresses first, so it hands out a URL
# no browser off this board can reach. Ask the routing table instead.
lan_ip() { ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}'; }

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn()  { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
fail()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

# The only trustworthy liveness test on this board: is the port listening?
port_open() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

wait_for_port() {
  local port=$1 name=$2 waited=0
  while [ "$waited" -lt "$WAIT_SECONDS" ]; do
    port_open "$port" && return 0
    sleep 1
    waited=$((waited + 1))
    [ $((waited % 10)) -eq 0 ] && info "still waiting for $name (${waited}s)"
  done
  return 1
}

# Opening a node proves nothing - the Venus codecs open and then return no
# frames. Only a real frame counts.
detect_camera() {
  "$RECOGNITION_PY" - <<'CAMPROBE' 2>/dev/null
import cv2

for idx in range(8):
    cap = cv2.VideoCapture(idx)
    if not cap.isOpened():
        cap.release()
        continue
    ok, frame = cap.read()
    cap.release()
    if ok and frame is not None:
        print(idx)
        break
CAMPROBE
}

console_state() {
  arduino-app-cli app list 2>/dev/null \
    | awk -v p="signally-console" '$0 ~ p { print $(NF-1) }' | head -1
}

# ---------------------------------------------------------------------------

preflight() {
  local bad=0
  [ -x "$RECOGNITION_PY" ]  || { fail "no recognition venv at $RECOGNITION_PY"; bad=1; }
  [ -d "$RECOGNITION_DIR/src/recognition" ] || { fail "no recognition source in $RECOGNITION_DIR"; bad=1; }
  mkdir -p "$TAKES_DIR" || { fail "cannot create takes dir $TAKES_DIR"; bad=1; }
  [ -f "$PHRASES" ] || { fail "no phrases.json at $PHRASES"; bad=1; }
  [ -d "$ORCHESTRATOR_DIR/src/orchestrator" ] || { fail "no orchestrator source in $ORCHESTRATOR_DIR"; bad=1; }
  if [ "$CAMERA" != "auto" ]; then
    [ -e "/dev/video$CAMERA" ] || { fail "/dev/video$CAMERA does not exist"; bad=1; }
  fi
  command -v arduino-app-cli >/dev/null || warn "arduino-app-cli missing — console cannot start"
  return $bad
}

start_recognition() {
  if port_open "$REC_PORT"; then
    warn "recognition already listening on $REC_PORT — leaving it alone"
    warn "(only one process can hold the camera; starting a second gives you a confusing fault)"
    return 0
  fi
  if [ "$CAMERA" = "auto" ]; then
    info "probing for a camera node that actually delivers frames"
    CAMERA=$(detect_camera)
    if [ -z "$CAMERA" ]; then
      fail "no video node delivered a frame - is the camera plugged in?"
      return 1
    fi
    ok "camera is /dev/video$CAMERA"
  fi
  info "starting recognition on camera $CAMERA at $RESOLUTION (xnnpack threads: $MP_THREADS)"
  ( cd "$RECOGNITION_DIR" && PYTHONPATH=src nohup "$RECOGNITION_PY" -m recognition \
      --camera "$CAMERA" --resolution "$RESOLUTION" --view-port "$VIEW_PORT" \
      --unlabelled-root "$TAKES_DIR" --num-threads "$MP_THREADS" \
      --classifier "$CLASSIFIER" \
      > "$LOG_DIR/recognition.log" 2>&1 & )
  if wait_for_port "$REC_PORT" "recognition"; then
    ok "recognition up on $REC_PORT"
  else
    fail "recognition did not open $REC_PORT in ${WAIT_SECONDS}s"
    info "last lines of $LOG_DIR/recognition.log:"
    grep -viE "$NOISE" "$LOG_DIR/recognition.log" 2>/dev/null | tail -8 | sed 's/^/      /'
    return 1
  fi
}

start_orchestrator() {
  if port_open "$ORC_PORT"; then
    warn "orchestrator already listening on $ORC_PORT — leaving it alone"
    return 0
  fi
  info "starting orchestrator (system python3, no venv)"
  ( cd "$ORCHESTRATOR_DIR" && PYTHONPATH=src nohup python3 -m orchestrator \
      --phrases "$PHRASES" \
      > "$LOG_DIR/orchestrator.log" 2>&1 & )
  if wait_for_port "$ORC_PORT" "orchestrator"; then
    ok "orchestrator up on $ORC_PORT"
  else
    fail "orchestrator did not open $ORC_PORT in ${WAIT_SECONDS}s"
    tail -8 "$LOG_DIR/orchestrator.log" 2>/dev/null | sed 's/^/      /'
    return 1
  fi
}

start_console() {
  command -v arduino-app-cli >/dev/null || { warn "skipping console — no arduino-app-cli"; return 0; }
  [ -d "$CONSOLE_APP" ] || { warn "skipping console — $CONSOLE_APP not found"; return 0; }
  info "starting App Lab console"
  info "(first run downloads a Python into the container — this takes minutes,"
  info " and looks like a hang. Later starts are quick.)"
  if arduino-app-cli app start "$CONSOLE_APP" > "$LOG_DIR/console.log" 2>&1; then
    ok "console app started — http://$(lan_ip):7000"
  else
    fail "console app failed to start"
    tail -8 "$LOG_DIR/console.log" 2>/dev/null | sed 's/^/      /'
    return 1
  fi
}

# ---------------------------------------------------------------------------

stop_one() {
  local pattern=$1 port=$2 name=$3
  if ! port_open "$port"; then
    info "$name not running"
    return 0
  fi
  # Bracket the first character so this pattern cannot match its own command
  # line. Even so, trust only the port for the verdict.
  local bracketed="[${pattern:0:1}]${pattern:1}"
  pkill -f "$bracketed" 2>/dev/null || true
  for _ in $(seq 1 10); do
    port_open "$port" || { ok "$name stopped (port $port closed)"; return 0; }
    sleep 1
  done
  pkill -9 -f "$bracketed" 2>/dev/null || true
  sleep 2
  if port_open "$port"; then
    fail "$name still holding port $port after SIGKILL"
    return 1
  fi
  ok "$name stopped (needed SIGKILL)"
}

do_start() {
  mkdir -p "$LOG_DIR"
  bold "SignAlly — starting"
  preflight || { fail "preflight failed; not starting anything"; return 1; }
  start_recognition || return 1
  start_orchestrator || return 1
  start_console
  echo
  do_status
}

do_stop() {
  bold "SignAlly — stopping"
  if command -v arduino-app-cli >/dev/null && [ -d "$CONSOLE_APP" ]; then
    arduino-app-cli app stop "$CONSOLE_APP" >/dev/null 2>&1 && ok "console app stopped" \
      || info "console app was not running"
  fi
  stop_one "recognition --camera" "$REC_PORT" "recognition"
  stop_one "orchestrator" "$ORC_PORT" "orchestrator"
}

do_status() {
  bold "SignAlly — status"

  # python3 rather than grep: `state` and `tracking` are null until warmup
  # finishes, and a regex for a quoted string silently reports "?" for a field
  # that is answering perfectly well.
  probe() {
    curl -s --max-time 3 "localhost:$1/health" 2>/dev/null \
      | python3 -c "
import json, sys
try:
    h = json.load(sys.stdin)
except Exception:
    print('unreadable health'); raise SystemExit
print('  '.join(f'{k}={h.get(k)}' for k in sys.argv[1:]))
" "${@:2}" 2>/dev/null
  }

  if port_open "$REC_PORT"; then
    ok "recognition   :$REC_PORT   $(probe "$REC_PORT" ok fps threads state tracking)"
  else
    fail "recognition   :$REC_PORT   not listening"
  fi

  if port_open "$ORC_PORT"; then
    ok "orchestrator  :$ORC_PORT   $(probe "$ORC_PORT" ok recognition_connected screen subscribers)"
  else
    fail "orchestrator  :$ORC_PORT   not listening"
  fi

  local cstate
  cstate=$(console_state)
  case "$cstate" in
    running|RUNNING) ok   "console       App Lab   $cstate" ;;
    "")              warn "console       App Lab   unknown (app not listed)" ;;
    *)               fail "console       App Lab   $cstate" ;;
  esac

  echo
  info "console UI:  http://$(lan_ip):7000"
  if [ "$VIEW_PORT" != "0" ]; then
    if port_open "$VIEW_PORT"; then
      info "camera view: http://$(lan_ip):$VIEW_PORT/view   (streams only while open)"
    else
      warn "camera view: not listening on $VIEW_PORT"
    fi
  fi
  info "The CrowPanel is driven through the console app: starting it flashes the"
  info "relay in applab/signally-console/sketch/ onto the STM32, which carries"
  info "protocol lines to the panel over UART. With no panel answering, the"
  info "console falls back to standing in for it. 'Panel' on the page says which."
}

do_logs() {
  local which=${1:-all}
  case "$which" in
    rec|recognition) grep -viE "$NOISE" "$LOG_DIR/recognition.log" | tail -40 ;;
    orc|orchestrator) tail -40 "$LOG_DIR/orchestrator.log" ;;
    console) arduino-app-cli app logs "$CONSOLE_APP" 2>&1 | tail -40 ;;
    all)
      bold "recognition"; grep -viE "$NOISE" "$LOG_DIR/recognition.log" 2>/dev/null | tail -15
      echo; bold "orchestrator"; tail -15 "$LOG_DIR/orchestrator.log" 2>/dev/null
      ;;
    *) echo "unknown log: $which"; return 1 ;;
  esac
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; echo; do_start ;;
  status)  do_status ;;
  logs)    do_logs "${2:-all}" ;;
  *)
    echo "usage: $(basename "$0") {start|stop|restart|status|logs [recognition|orchestrator|console]}"
    exit 2
    ;;
esac
