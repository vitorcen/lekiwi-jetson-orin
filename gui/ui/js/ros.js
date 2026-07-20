// ROS 2 tab: sensor previews over rosbridge (WebSocket :9090, JSON protocol).
//
// No roslib dependency — the rosbridge wire format we need is 3 ops:
//   out: {op:"subscribe", id, topic, type, throttle_rate, queue_length}
//        {op:"unsubscribe", id, topic}
//   in : {op:"publish", topic, msg}
// The WebView talks to rosbridge directly (CSP is null, no Rust proxy needed):
// this is read-only telemetry — control stays on the ZMQ path.
//
// Four panels:
//  - LIDAR /scan (sensor_msgs/LaserScan): polar plot on canvas, heading-up
//    (REP-103: x forward, y left). Board node: ros2/ld19_lidar.py.
//  - Three CompressedImage JPEG previews sharing one code path (IMG_FEEDS):
//    depth (Astra OpenNI2 republish), front (forwarded from vlm-daemon, the
//    camera's single owner), wrist (direct UVC, opened only while subscribed).
//
// Connection lifecycle mirrors vision.js: tab enter arms, tab leave / page
// hidden stands down, offline probes retry every 2 s until rosbridge answers.
import { $, S } from './state.js';

const RETRY_MS = 2000;     // reconnect probe while wanted but unreachable
const STALE_S  = 2;        // badge flips to 停帧 when a feed goes quiet
const SCAN_THROTTLE_MS  = 100;   // 10 Hz is the native rate of common lidars
const IMG_THROTTLE_MS   = 100;
const SCAN_MAX_R = 6;      // clamp the plot radius (m) — indoor scale

let wantActive = true;     // user intent: false only after an explicit 断开
let active = false;        // tab visible AND wantActive
let ws = null;             // current WebSocket (null when down)
let retryTimer = null, ageTimer = null;
let subs = [];             // [{id, topic}] currently subscribed (for resub)

// JPEG image feeds all behave identically — one table, zero special cases.
// key doubles as the sub id suffix; element ids follow the <key>Img pattern.
const IMG_FEEDS = [
  { key: 'depth', topicEl: 'depthTopic', def: '/depth_preview/compressed', label: '深度' },
  { key: 'front', topicEl: 'frontTopic', def: '/front_cam/compressed',     label: '前视' },
  { key: 'wrist', topicEl: 'wristTopic', def: '/wrist_cam/compressed',     label: '腕部' },
];

// per-feed freshness: client clock of last message + EMA of intervals for hz
const feed = { scan: { at: 0, hz: 0 } };
for (const f of IMG_FEEDS) feed[f.key] = { at: 0, hz: 0 };

function topicOf(f) { return $(f.topicEl).value.trim() || f.def; }

function curUrl() {
  const ip = ($('rosip') && $('rosip').value.trim()) || '';
  const port = ($('rosport') && $('rosport').value.trim()) || '9090';
  return ip ? `ws://${ip}:${port}` : '';
}

function markFresh(f) {
  const now = Date.now();
  if (f.at) {
    const dt = (now - f.at) / 1000;
    if (dt > 0) f.hz = f.hz ? f.hz * 0.8 + (1 / dt) * 0.2 : 1 / dt;
  }
  f.at = now;
}

// ---- connection state pill ----------------------------------------------

function setPill(text, cls) {
  const el = $('rosState');
  if (el) { el.textContent = text; el.className = 'pill ' + cls; }
}

// ---- rosbridge client ----------------------------------------------------

function send(o) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o));
}

function subscribe(id, topic, type, throttle) {
  subs.push({ id, topic });
  send({ op: 'subscribe', id, topic, type,
         throttle_rate: throttle, queue_length: 1 });
}

function resubscribeAll() {
  for (const s of subs.splice(0)) send({ op: 'unsubscribe', id: s.id, topic: s.topic });
  subscribe('sub:scan', $('scanTopic').value.trim() || '/scan',
            'sensor_msgs/msg/LaserScan', SCAN_THROTTLE_MS);
  for (const f of IMG_FEEDS)
    subscribe('sub:' + f.key, topicOf(f),
              'sensor_msgs/msg/CompressedImage', IMG_THROTTLE_MS);
}

function connect() {
  if (ws || !active) return;
  const url = curUrl();
  if (!url) { setPill('未配置板子IP', 'warn'); return; }
  setPill('连接中…', 'warn');
  let sock;
  try { sock = new WebSocket(url); }
  catch { scheduleRetry(); return; }
  ws = sock;
  sock.onopen = () => {
    if (ws !== sock) return;
    setPill('已连接', 'ok');
    resubscribeAll();
  };
  sock.onmessage = ev => {
    if (ws !== sock) return;
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.op !== 'publish') return;
    if (m.topic === ($('scanTopic').value.trim() || '/scan')) { paintScan(m.msg); return; }
    const f = IMG_FEEDS.find(x => m.topic === topicOf(x));
    if (f) paintImage(f, m.msg);
  };
  // onerror always precedes onclose — one handler owns teardown + retry
  sock.onclose = () => { if (ws === sock) { ws = null; feedDown(); scheduleRetry(); } };
  sock.onerror = () => { try { sock.close(); } catch { /* already dead */ } };
}

function disconnect() {
  const sock = ws;
  ws = null;
  subs = [];
  if (sock) try { sock.close(); } catch { /* already dead */ }
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  feedDown();
  setPill('未连接', 'bad');
}

function scheduleRetry() {
  if (!active) return;
  setPill('未连接', 'bad');
  if (!retryTimer) retryTimer = setTimeout(() => { retryTimer = null; connect(); }, RETRY_MS);
}

// ---- lidar polar plot ----------------------------------------------------

function paintScan(msg) {
  markFresh(feed.scan);
  const cv = $('scanCanvas');
  if (!cv || !msg || !msg.ranges) return;
  $('scanPh').style.display = 'none';

  // match backing store to CSS size (box is square via aspect-ratio)
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth * dpr, h = cv.clientHeight * dpr;
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext('2d');
  const cx = w / 2, cy = h / 2;
  const maxR = Math.min(msg.range_max || SCAN_MAX_R, SCAN_MAX_R);
  const k = (Math.min(w, h) / 2 - 8 * dpr) / maxR;   // px per meter

  ctx.clearRect(0, 0, w, h);

  // range rings every meter + faint cross
  ctx.strokeStyle = '#1c2030'; ctx.fillStyle = '#4f5678';
  ctx.lineWidth = dpr; ctx.font = `${10 * dpr}px ui-monospace, monospace`;
  for (let r = 1; r <= maxR; r++) {
    ctx.beginPath(); ctx.arc(cx, cy, r * k, 0, Math.PI * 2); ctx.stroke();
    ctx.fillText(r + 'm', cx + 3 * dpr, cy - r * k + 11 * dpr);
  }
  ctx.beginPath();
  ctx.moveTo(cx, cy - maxR * k); ctx.lineTo(cx, cy + maxR * k);
  ctx.moveTo(cx - maxR * k, cy); ctx.lineTo(cx + maxR * k, cy);
  ctx.stroke();

  // points: REP-103 x forward / y left → screen up / screen left
  let n = 0, nearest = Infinity;
  ctx.fillStyle = '#89b4fa';
  for (let i = 0; i < msg.ranges.length; i++) {
    const r = msg.ranges[i];
    if (!(r >= msg.range_min && r <= maxR)) continue;   // NaN/0/inf all fail
    const a = msg.angle_min + i * msg.angle_increment;
    const px = cx - Math.sin(a) * r * k;
    const py = cy - Math.cos(a) * r * k;
    ctx.fillRect(px - dpr, py - dpr, 2 * dpr, 2 * dpr);
    n++;
    if (r < nearest) nearest = r;
  }

  // robot marker: triangle pointing up (heading)
  ctx.fillStyle = '#f9e2af';
  ctx.beginPath();
  ctx.moveTo(cx, cy - 7 * dpr);
  ctx.lineTo(cx - 5 * dpr, cy + 5 * dpr);
  ctx.lineTo(cx + 5 * dpr, cy + 5 * dpr);
  ctx.closePath(); ctx.fill();

  $('scanMeta').textContent =
    `${n} 点 · ${feed.scan.hz.toFixed(1)} Hz · 最近 ${isFinite(nearest) ? nearest.toFixed(2) + 'm' : '—'}`;
}

// ---- image previews (depth / front / wrist) ------------------------------

function paintImage(f, msg) {
  if (!msg || !msg.data) return;
  markFresh(feed[f.key]);
  const img = $(f.key + 'Img');
  const fmt = (msg.format || '').includes('png') ? 'png' : 'jpeg';
  img.src = `data:image/${fmt};base64,` + msg.data;
  img.classList.add('live');
  $(f.key + 'Ph').style.display = 'none';
  $(f.key + 'Meta').textContent = `${feed[f.key].hz.toFixed(1)} fps`;
}

// ---- freshness badges (one authority, ticked at 2 Hz) --------------------

function badge(id, text, cls) {
  const el = $(id);
  if (el) { el.textContent = text; el.className = 'vbadge ' + cls; }
}

function tickAge() {
  const connected = ws && ws.readyState === WebSocket.OPEN;
  const rows = [['scan', 'scanBadge', '/scan'],
                ...IMG_FEEDS.map(x => [x.key, x.key + 'Badge', x.label])];
  for (const [key, bid, label] of rows) {
    const f = feed[key];
    if (!connected || !f.at) { badge(bid, '未接入', 'bad'); continue; }
    const age = (Date.now() - f.at) / 1000;
    if (age > STALE_S) badge(bid, `停帧 ${age.toFixed(0)}s`, 'warn');
    else badge(bid, label + ' 实时', 'ok');
  }
}

function feedDown() {
  feed.scan.at = 0; feed.scan.hz = 0;
  $('scanPh').style.display = '';
  $('scanMeta').textContent = '—';
  for (const f of IMG_FEEDS) {
    feed[f.key].at = 0; feed[f.key].hz = 0;
    $(f.key + 'Ph').style.display = '';
    $(f.key + 'Img').classList.remove('live');
    $(f.key + 'Meta').textContent = '—';
  }
  tickAge();
}

// ---- activation ----------------------------------------------------------

function startActive() {
  if (active || !wantActive || S.page !== 'ros' || document.hidden) return;
  active = true;
  if (!ageTimer) ageTimer = setInterval(tickAge, 500);
  connect();
}

function stopActive() {
  if (!active) return;
  active = false;
  if (ageTimer) { clearInterval(ageTimer); ageTimer = null; }
  disconnect();
}

export function onEnterRos() { startActive(); }
export function onLeaveRos() { stopActive(); }

// ---- wiring --------------------------------------------------------------

$('rosConnBtn').onclick = () => {
  wantActive = !wantActive;
  $('rosConnBtn').textContent = wantActive ? '断开' : '连接';
  $('rosConnBtn').classList.toggle('live', !wantActive);
  if (wantActive) startActive();
  else stopActive();
};

// topic edits take effect live on an open connection
for (const id of ['scanTopic', 'depthTopic', 'frontTopic', 'wristTopic']) {
  const el = $(id);
  if (el) el.addEventListener('change', () => {
    if (ws && ws.readyState === WebSocket.OPEN) resubscribeAll();
  });
}

// connection endpoint edits force a reconnect
for (const id of ['rosip', 'rosport']) {
  const el = $(id);
  if (el) el.addEventListener('change', () => {
    if (active) { disconnect(); connect(); }
  });
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { if (S.page === 'ros') stopActive(); }
  else if (S.page === 'ros') startActive();
});
