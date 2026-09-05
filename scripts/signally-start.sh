#!/usr/bin/env bash
#
# Start, stop and inspect the whole SignAlly stack on the UNO Q.
#
# Three processes, and the point of this script is that "are all three running?"
# becomes something you verify rather than assume:
#
#   1. recognition   host, ~/recognition-venv (Python 3.12), holds the camera
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

RECOGNITION_DIR="${RECOGNITION_DIR:-$HOME/islkit}"
RECOGNITION_PY="${RECOGNITION_PY:-$HOME/recognition-venv/bin/python}"
ORCHESTRATOR_DIR="${ORCHESTRATOR_DIR:-$HOME/SignAlly}"
CONSOLE_APP="${CONSOLE_APP:-$HOME/SignAlly/applab/signally-console}"
LOG_DIR="${LOG_DIR:-$HOME/logs}"

# /dev/video1, not video2. The attached camera is a Sony UVC device that
# enumerates video1..video4; only video1 delivers frames. Override with CAMERA=n.
CAMERA="${CAMERA:-1}"
RESOLUTION="${RESOLUTION:-640x480}"

REC_PORT=9978
ORC_PORT=9977
WAIT_SECONDS=45

# MediaPipe writes a wall of noise to stderr on every start.
NOISE='^W0|absl|xnnpack|cpuinfo|feedback|landmark_projection|Fiber init|TensorFlow Lite'

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

console_state() {
  arduino-app-cli app list 2>/dev/null \
    | awk -v p="signally-console" '$0 ~ p { print $(NF-1) }' | head -1
}

# ---------------------------------------------------------------------------

preflight() {
  local bad=0
  [ -x "$RECOGNITION_PY" ]  || { fail "no recognition venv at $RECOGNITION_PY"; bad=1; }
  [ -d "$RECOGNITION_DIR" ] || { fail "no recognition repo at $RECOGNITION_DIR"; bad=1; }
  [ -f "$ORCHESTRATOR_DIR/phrases.json" ] || { fail "no phrases.json in $ORCHESTRATOR_DIR"; bad=1; }
  [ -d "$ORCHESTRATOR_DIR/src/orchestrator" ] || { fail "no orchestrator source in $ORCHESTRATOR_DIR"; bad=1; }
  [ -e "/dev/video$CAMERA" ] || { fail "/dev/video$CAMERA does not exist"; bad=1; }
  command -v arduino-app-cli >/dev/null || warn "arduino-app-cli missing — console cannot start"
  return $bad
}

start_recognition() {
  if port_open "$REC_PORT"; then
    warn "recognition already listening on $REC_PORT — leaving it alone"
    warn "(only one process can hold the camera; starting a second gives you a confusing fault)"
    return 0
  fi
  info "starting recognition on camera $CAMERA at $RESOLUTION"
  ( cd "$RECOGNITION_DIR" && nohup "$RECOGNITION_PY" experiments/serve.py \
      --camera "$CAMERA" --resolution "$RESOLUTION" \
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
  if arduino-app-cli app start "$CONSOLE_APP" > "$LOG_DIR/console.log" 2>&1; then
    ok "console app started"
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
  stop_one "serve.py" "$REC_PORT" "recognition"
  stop_one "orchestrator" "$ORC_PORT" "orchestrator"
}

do_status() {
  bold "SignAlly — status"

  if port_open "$REC_PORT"; then
    local health fps state
    health=$(curl -s --max-time 3 "localhost:$REC_PORT/health" 2>/dev/null)
    fps=$(printf '%s' "$health" | grep -o '"fps": *[0-9.]*' | head -1 | grep -o '[0-9.]*$')
    state=$(printf '%s' "$health" | grep -o '"state": *"[a-z]*"' | head -1 | grep -o '"[a-z]*"$' | tr -d '"')
    ok "recognition   :$REC_PORT   fps=${fps:-?}  state=${state:-?}"
  else
    fail "recognition   :$REC_PORT   not listening"
  fi

  if port_open "$ORC_PORT"; then
    local h connected screen
    h=$(curl -s --max-time 3 "localhost:$ORC_PORT/health" 2>/dev/null)
    connected=$(printf '%s' "$h" | grep -o '"recognition_connected": *[a-z]*' | grep -o '[a-z]*$')
    screen=$(printf '%s' "$h" | grep -o '"screen": *"[a-z]*"' | grep -o '"[a-z]*"$' | tr -d '"')
    ok "orchestrator  :$ORC_PORT   recognition_connected=${connected:-?}  screen=${screen:-?}"
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
  info "The physical CrowPanel is NOT part of this stack. The console app stands"
  info "in for it; the MCU sketch is untouched and its Bridge path is disabled."
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
