/**
 * SignAlly console — browser side.
 *
 * Grew out of the UART test harness's control surface, with one change of
 * substance: that page talked to an invented `/api/bridge` endpoint and drove
 * the MCU's mock state machine. This one talks to the App Lab WebUI brick over
 * its websocket, and the states it shows are the orchestrator's real ones.
 *
 * Events from Python:
 *   status  {host, linked, auto_ack, screen, last_seq, panel}
 *   line    {dir: 'down'|'up', msg, note, t}
 *   history {lines: [line, ...]}       replayed on connect
 *
 * Events to Python:
 *   button        {b: 'start'|'stop'|'mute', on?}
 *   hello         {}
 *   set_auto_ack  {on}
 */

'use strict';

const LOG_MAX_LINES = 300;

const el = {
  badge: document.getElementById('conn-badge'),
  host: document.getElementById('host-text'),
  autoack: document.getElementById('chk-autoack'),
  panel: document.getElementById('panel-badge'),
  hello: document.getElementById('btn-hello'),
  start: document.getElementById('btn-start'),
  stop: document.getElementById('btn-stop'),
  mute: document.getElementById('btn-mute'),
  clear: document.getElementById('btn-clear'),
  log: document.getElementById('wire-log'),
  sim: document.getElementById('panel-sim'),
  eyebrow: document.getElementById('panel-eyebrow'),
  headline: document.getElementById('panel-headline'),
  detail: document.getElementById('panel-detail'),
  steps: Array.from(document.querySelectorAll('.step'))
};

let muted = false;
const logLines = [];

const ui = new WebUI();

/* ------------------------------------------------------------------------
 * Rendering
 * ---------------------------------------------------------------------- */

function pad(n, width) {
  return String(n).padStart(width, ' ');
}

function stamp(t) {
  const d = t ? new Date(t * 1000) : new Date();
  return d.toTimeString().slice(0, 8);
}

function appendLine(entry) {
  const arrow = entry.dir === 'down' ? '<--' : '-->';
  const seq = entry.msg && entry.msg.seq !== undefined ? pad(entry.msg.seq, 4) : '   -';
  const note = entry.note ? `   ${entry.note}` : '';
  logLines.push(`${stamp(entry.t)} ${arrow} ${seq}  ${JSON.stringify(entry.msg)}${note}`);
  if (logLines.length > LOG_MAX_LINES) logLines.shift();
  el.log.textContent = logLines.join('\n');
  el.log.scrollTop = el.log.scrollHeight;
}

/** Highlight the step the panel would be on. */
function markStep(screen) {
  el.steps.forEach((s) => {
    s.classList.toggle('step-active', s.dataset.state === screen);
  });
}

/**
 * Draw what the panel would show.
 *
 * The panel owns its own copy strings — "Didn't catch that" is a panel-side
 * string, not something the orchestrator sends — so they live here too.
 */
function drawScreen(screen, msg) {
  const copy = {
    boot: ['SCREEN', 'SignAlly', 'Booting.'],
    idle: ['IDLE', 'Tap Start to begin', 'The camera is not capturing.'],
    listening: ['LISTENING', 'Sign now', 'Watching for a gesture.'],
    analyzing: ['ANALYZING', 'Thinking…', 'Running the classifier.'],
    result: ['RESULT', '', ''],
    unclear: ['UNCLEAR', "Didn't catch that", 'Try the sign again.'],
    error: ['ERROR', '', '']
  };

  const [eyebrow, headline, detail] = copy[screen] || ['SCREEN', screen, ''];
  el.sim.dataset.screen = screen;
  el.eyebrow.textContent = eyebrow;

  if (screen === 'result' && msg) {
    el.headline.textContent = msg.text || msg.id || '(no text)';
    el.detail.textContent = `id: ${msg.id}   conf: ${msg.conf}`;
  } else if (screen === 'unclear' && msg) {
    el.headline.textContent = headline;
    el.detail.textContent = `conf: ${msg.conf}`;
  } else if (screen === 'error' && msg) {
    el.headline.textContent = msg.text || 'Something broke';
    el.detail.textContent = '';
  } else {
    el.headline.textContent = headline;
    el.detail.textContent = detail;
  }

  markStep(screen);
}

/* ------------------------------------------------------------------------
 * Messages from Python
 * ---------------------------------------------------------------------- */

ui.on_message('status', (s) => {
  const on = !!s.linked;
  el.badge.textContent = on ? 'connected' : 'disconnected';
  el.badge.className = on ? 'badge badge-on' : 'badge badge-off';
  el.host.textContent = s.host
    ? `${s.host}:9977` + (s.last_seq !== null && s.last_seq !== undefined ? `  ·  last seq ${s.last_seq}` : '')
    : 'no host found';
  el.autoack.checked = !!s.auto_ack;

  /* A real panel on the far end of the Bridge acks for itself, so auto-ack is
   * forced off and the toggle is locked -- turning it back on would report every
   * message as having landed twice. */
  const panel = s.panel;
  el.panel.textContent = panel ? String(panel) : 'stand-in';
  el.panel.className = panel ? 'badge badge-on' : 'badge badge-off';
  el.autoack.disabled = !!panel;
  el.autoack.title = panel
    ? 'The panel is acking for itself'
    : 'Ack every seq, the way the panel firmware does';
});

ui.on_message('line', (entry) => {
  appendLine(entry);
  if (entry.dir !== 'down') return;

  const msg = entry.msg || {};
  if (msg.t === 'state' && msg.s) drawScreen(msg.s, msg);
  else if (msg.t === 'result') drawScreen('result', msg);
  else if (msg.t === 'unclear') drawScreen('unclear', msg);
  else if (msg.t === 'error') drawScreen('error', msg);
});

ui.on_message('history', (data) => {
  logLines.length = 0;
  (data.lines || []).forEach(appendLine);
  const lastDown = (data.lines || []).filter((l) => l.dir === 'down').pop();
  if (lastDown) {
    const msg = lastDown.msg || {};
    if (msg.t === 'state' && msg.s) drawScreen(msg.s, msg);
    else if (msg.t === 'result') drawScreen('result', msg);
  }
});

ui.on_connect(() => console.log('console connected to App Lab'));
ui.on_disconnect(() => {
  el.badge.textContent = 'app lab lost';
  el.badge.className = 'badge badge-off';
});

/* ------------------------------------------------------------------------
 * Controls
 * ---------------------------------------------------------------------- */

el.start.addEventListener('click', () => ui.send_message('button', { b: 'start' }));
el.stop.addEventListener('click', () => ui.send_message('button', { b: 'stop' }));

el.mute.addEventListener('click', () => {
  muted = !muted;
  el.mute.textContent = `MUTE: ${muted ? 'on' : 'off'}`;
  ui.send_message('button', { b: 'mute', on: muted });
});

el.hello.addEventListener('click', () => ui.send_message('hello', {}));

el.autoack.addEventListener('change', () => {
  ui.send_message('set_auto_ack', { on: el.autoack.checked });
});

el.clear.addEventListener('click', () => {
  logLines.length = 0;
  el.log.textContent = '(cleared)';
});

drawScreen('boot', null);
