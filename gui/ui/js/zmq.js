// ZeroMQ tab: connect to lekiwi_host and drive the base from the keyboard.
//
// The Rust backend owns the actual ZMQ PUSH socket; here we only frame intent:
// maintain the set of held keys, and at 20 Hz turn them into an
// {x.vel, y.vel, theta.vel} command that Rust forwards to the host. The host
// has an idle watchdog, so we send a zero the moment all keys release and never
// stream from an unfocused/hidden tab (dead-man safety).
import { $, S, invoke } from './state.js';

// Speed levels mirror lerobot's LeKiwi defaults exactly (xy m/s, theta deg/s).
const LEVELS = [
  { xy: 0.10, th: 30 },
  { xy: 0.25, th: 60 },
  { xy: 0.40, th: 90 },
];
let speedIdx = 1;

// key -> [x sign (forward+), y sign (left+), theta sign (CCW+)]
// Matches the on-screen pad: WASD translate, QE rotate. Arrow keys mirror WASD
// (↑↓ forward/back, ←→ strafe) so either hand position drives the base.
const MAP = {
  w: [1, 0, 0], s: [-1, 0, 0],
  a: [0, 1, 0], d: [0, -1, 0],
  q: [0, 0, 1], e: [0, 0, -1],
  arrowup: [1, 0, 0], arrowdown: [-1, 0, 0],
  arrowleft: [0, 1, 0], arrowright: [0, -1, 0],
};
// Arrow keys light their WASD twin on the pad and show as glyphs in the list.
const ALIAS = { arrowup: 'w', arrowdown: 's', arrowleft: 'a', arrowright: 'd' };
const GLYPH = { arrowup: '↑', arrowdown: '↓', arrowleft: '←', arrowright: '→', ' ': '␣' };

const HZ = 20;
const keysDown = new Set();
let timer = null;
let connected = false;

// ---- connection ----------------------------------------------------------

async function connect() {
  if (!invoke) { setState('无 Tauri 后端', 'bad'); return; }
  if (connected) { await disconnect(); return; }
  const ip = $('ip').value.trim();
  const port = parseInt($('port').value.trim(), 10);
  if (!ip || !port) { setState('IP/端口无效', 'bad'); return; }
  setState('连接中…', 'warn');
  try {
    const ep = await invoke('zmq_connect', { ip, port });
    connected = true;
    setState('已连接 ' + ep, 'ok');
    $('connBtn').textContent = '断开';
    $('connBtn').classList.add('live');
  } catch (e) {
    setState('连接失败: ' + e, 'bad');
  }
}

async function disconnect() {
  stopStream();
  if (invoke) { try { await invoke('zmq_disconnect'); } catch { /* ignore */ } }
  connected = false;
  setState('未连接', 'bad');
  $('connBtn').textContent = '连接';
  $('connBtn').classList.remove('live');
}

function setState(text, cls) {
  const a = $('zmqState'), b = $('connPill');
  for (const el of [a, b]) { el.textContent = text; el.className = 'pill ' + cls; }
}

// ---- command stream ------------------------------------------------------

function compute() {
  let x = 0, y = 0, t = 0;
  for (const k of keysDown) { const m = MAP[k]; if (m) { x += m[0]; y += m[1]; t += m[2]; } }
  const L = LEVELS[speedIdx];
  return {
    x: Math.sign(x) * L.xy,
    y: Math.sign(y) * L.xy,
    theta: Math.sign(t) * L.th,
  };
}

async function tick() {
  const { x, y, theta } = compute();
  render(x, y, theta);
  if (connected && invoke) {
    try { await invoke('zmq_send_base', { x, y, theta }); }
    catch (e) { setState('发送失败: ' + e, 'bad'); }
  }
}

function startStream() {
  if (!timer) { tick(); timer = setInterval(tick, 1000 / HZ); }
}

// Stop streaming and command a single zero so the base halts at once.
function stopStream() {
  if (timer) { clearInterval(timer); timer = null; }
  keysDown.clear();
  render(0, 0, 0);
  refreshKeys();
  if (connected && invoke) { invoke('zmq_send_base', { x: 0, y: 0, theta: 0 }).catch(() => {}); }
}

// ---- visualization -------------------------------------------------------

function render(x, y, theta) {
  $('vx').textContent = x.toFixed(2);
  $('vy').textContent = y.toFixed(2);
  $('vt').textContent = theta.toFixed(0);

  // Translation arrow: forward(+x) = up on screen, left(+y) = left on screen.
  const maxXY = 0.40, scale = 70;
  const ex = 120 - (y / maxXY) * scale;
  const ey = 120 - (x / maxXY) * scale;
  const arrow = $('vArrow');
  arrow.setAttribute('x2', ex.toFixed(1));
  arrow.setAttribute('y2', ey.toFixed(1));
  arrow.style.opacity = (x || y) ? '1' : '0.15';

  // Rotation arc: a short arc near the rim, direction by sign of theta.
  const spin = $('vSpin');
  if (theta) {
    const ccw = theta > 0;                       // +theta = CCW
    const r = 100, a0 = -Math.PI / 2;
    const sweep = (ccw ? -1 : 1) * (Math.PI * 0.7);
    const a1 = a0 + sweep;
    const p = (a) => [120 + r * Math.cos(a), 120 + r * Math.sin(a)];
    const [sx, sy] = p(a0), [fx, fy] = p(a1);
    const large = 0, sf = ccw ? 0 : 1;
    spin.setAttribute('d', `M ${sx.toFixed(1)} ${sy.toFixed(1)} A ${r} ${r} 0 ${large} ${sf} ${fx.toFixed(1)} ${fy.toFixed(1)}`);
    spin.style.opacity = '1';
  } else {
    spin.style.opacity = '0';
  }
}

function refreshKeys() {
  // A pad key lights when held directly or via its arrow-key alias.
  const lit = new Set();
  for (const k of keysDown) lit.add(ALIAS[k] || k);
  document.querySelectorAll('#keypad kbd').forEach(el =>
    el.classList.toggle('hit', lit.has(el.dataset.k)));
  const list = [...keysDown].map(k => GLYPH[k] || k.toUpperCase()).join(' + ');
  $('pressed').querySelector('span').textContent = list || '—';
}

// ---- speed selector ------------------------------------------------------

function setSpeed(i) {
  speedIdx = Math.max(0, Math.min(2, i));
  document.querySelectorAll('.spd').forEach(b =>
    b.classList.toggle('on', +b.dataset.i === speedIdx));
}

// ---- keyboard ------------------------------------------------------------

const pad = $('keypad');
const tele = $('telewrap');   // focus target: click anywhere in the panel to arm

function armFocus() { tele.classList.add('armed'); pad.classList.add('focus'); }
function dropFocus() { tele.classList.remove('armed'); pad.classList.remove('focus'); stopStream(); }

function onKeyDown(e) {
  if (S.page !== 'zmq' || !tele.classList.contains('armed')) return;
  const t = e.target.tagName;
  if (t === 'INPUT' || t === 'TEXTAREA') return;
  const k = e.key === ' ' ? ' ' : e.key.toLowerCase();

  if (k === ' ') { e.preventDefault(); flashSpace(); stopStream(); return; }   // 急停
  if (k === 'r') { e.preventDefault(); setSpeed(speedIdx + 1); return; }
  if (k === 'f') { e.preventDefault(); setSpeed(speedIdx - 1); return; }
  if (!MAP[k]) return;
  e.preventDefault();
  if (e.repeat) return;
  keysDown.add(k);
  refreshKeys();
  startStream();
}

function onKeyUp(e) {
  const k = e.key === ' ' ? ' ' : e.key.toLowerCase();
  if (!keysDown.delete(k)) return;
  refreshKeys();
  if (!keysDown.size) stopStream();
}

function flashSpace() {
  const el = document.querySelector('#keypad kbd[data-k=" "]');
  if (!el) return;
  el.classList.add('hit');
  setTimeout(() => el.classList.remove('hit'), 150);
}

// ---- exported hook for tab switching -------------------------------------

export function onLeaveZmq() { dropFocus(); }

// ---- wiring --------------------------------------------------------------

$('connBtn').onclick = connect;
document.querySelectorAll('.spd').forEach(b => b.onclick = () => setSpeed(+b.dataset.i));

// The whole teleop panel is the focus/keyboard target — clicking the keypad OR
// the motion-status side both arm keyboard control.
tele.tabIndex = 0;
tele.addEventListener('focus', armFocus);
tele.addEventListener('blur', dropFocus);
tele.addEventListener('click', () => tele.focus());

window.addEventListener('keydown', onKeyDown);
window.addEventListener('keyup', onKeyUp);
window.addEventListener('blur', dropFocus);           // window lost focus = release
document.addEventListener('visibilitychange', () => { if (document.hidden) dropFocus(); });

render(0, 0, 0);
