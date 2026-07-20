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
const IMU_THROTTLE_MS   = 100;   // dashboard redraw rate; node runs ~25 Hz
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
const feed = { scan: { at: 0, hz: 0 }, imu: { at: 0, hz: 0 } };
for (const f of IMG_FEEDS) feed[f.key] = { at: 0, hz: 0 };

function topicOf(f) { return $(f.topicEl).value.trim() || f.def; }
function imuPrefix() { return $('imuTopic').value.trim() || '/imu'; }

// Source-side truth: each board node self-reports its real publish fps on
// /diagnostics (1 Hz) — the only rate rosbridge throttling cannot distort.
const DIAG_KEY = { depth_preview: 'depth', front_cam: 'front',
                   wrist_cam: 'wrist', ld19_lidar: 'scan', imu_10dof: 'imu' };
const DIAG_STALE_MS = 3000;

function handleDiag(msg) {
  for (const st of msg.status || []) {
    const key = DIAG_KEY[st.name];
    if (!key) continue;
    const f = feed[key];
    for (const v of st.values || []) {
      if (v.key === 'fps') f.src = parseFloat(v.value);
      else if (v.key === 'cap_fps') f.cap = parseFloat(v.value);
    }
    f.srcAt = Date.now();
  }
}

// 采集 = sensor/camera real delivery rate, 发布 = node's ROS output rate —
// both self-measured on the board, both go stale (hidden) if diag stops.
function srcLabel(key) {
  const f = feed[key];
  if (!f.srcAt || Date.now() - f.srcAt >= DIAG_STALE_MS) return '';
  const cap = f.cap != null ? `采集 ${f.cap.toFixed(1)} · ` : '';
  return `${cap}发布 ${f.src.toFixed(1)} · `;
}

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
  const ip = imuPrefix();
  subscribe('sub:imu',   ip + '/data',     'sensor_msgs/msg/Imu',           IMU_THROTTLE_MS);
  subscribe('sub:imag',  ip + '/mag',      'sensor_msgs/msg/MagneticField', 500);
  subscribe('sub:itemp', ip + '/temp',     'sensor_msgs/msg/Temperature',   1000);
  subscribe('sub:ipress', ip + '/pressure', 'sensor_msgs/msg/FluidPressure', 1000);
  subscribe('sub:diag', '/diagnostics', 'diagnostic_msgs/msg/DiagnosticArray', 0);
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
    if (m.topic === '/diagnostics') { handleDiag(m.msg); return; }
    if (m.topic === ($('scanTopic').value.trim() || '/scan')) { paintScan(m.msg); return; }
    const ipfx = imuPrefix();
    if (m.topic === ipfx + '/data')     { paintImu(m.msg); return; }
    if (m.topic === ipfx + '/mag')      { imuState.mag = m.msg.magnetic_field; return; }
    if (m.topic === ipfx + '/temp')     { imuState.temp = m.msg.temperature; return; }
    if (m.topic === ipfx + '/pressure') { imuState.press = m.msg.fluid_pressure; return; }
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

  // scan is NOT throttled below its native 10 Hz, so this one is the real rate
  $('scanMeta').textContent =
    `${n} 点 · ${feed.scan.hz.toFixed(1)} Hz · 最近 ${isFinite(nearest) ? nearest.toFixed(2) + 'm' : '—'}`;
}

// ---- IMU dashboard -------------------------------------------------------
// One square canvas: artificial horizon (roll/pitch) + compass card (yaw),
// centered-zero bars for gyro/accel, text row for mag / temp / pressure.
// Euler is derived here from the quaternion — /imu/data is the only truth.

const imuState = { mag: null, temp: null, press: null };

function eulerOf(q) {
  const { w, x, y, z } = q;
  return {
    roll:  Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
    pitch: Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))),
    yaw:   Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
  };
}

function dialHorizon(ctx, cx, cy, r, roll, pitch, dpr) {
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.clip();
  ctx.translate(cx, cy); ctx.rotate(-roll);
  const py = Math.max(-r, Math.min(r, pitch / (Math.PI / 4) * r)); // 45° = r
  ctx.fillStyle = '#2c4a7c';                      // sky
  ctx.fillRect(-r * 2, -r * 2, r * 4, r * 4);
  ctx.fillStyle = '#5c4326';                      // ground
  ctx.fillRect(-r * 2, py, r * 4, r * 4);
  ctx.strokeStyle = '#e6e9f5'; ctx.lineWidth = 1.5 * dpr;
  ctx.beginPath(); ctx.moveTo(-r, py); ctx.lineTo(r, py); ctx.stroke();
  // pitch ladder every 15°
  ctx.strokeStyle = 'rgba(230,233,245,.5)'; ctx.lineWidth = dpr;
  for (const d of [-30, -15, 15, 30]) {
    const ly = py + d / 45 * r;
    ctx.beginPath(); ctx.moveTo(-r * 0.35, ly); ctx.lineTo(r * 0.35, ly); ctx.stroke();
  }
  ctx.restore();
  // fixed aircraft marker + bezel
  ctx.strokeStyle = '#f9e2af'; ctx.lineWidth = 2 * dpr;
  ctx.beginPath();
  ctx.moveTo(cx - r * 0.5, cy); ctx.lineTo(cx - r * 0.15, cy);
  ctx.moveTo(cx + r * 0.15, cy); ctx.lineTo(cx + r * 0.5, cy);
  ctx.stroke();
  ctx.fillStyle = '#f9e2af';
  ctx.fillRect(cx - 1.5 * dpr, cy - 1.5 * dpr, 3 * dpr, 3 * dpr);
  ctx.strokeStyle = '#3d4460'; ctx.lineWidth = dpr;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
}

function dialCompass(ctx, cx, cy, r, yaw, dpr) {
  // REP-103 yaw is CCW-positive; a compass card turns so heading sits on top
  const heading = ((-yaw * 180 / Math.PI) % 360 + 360) % 360;
  ctx.strokeStyle = '#3d4460'; ctx.lineWidth = dpr;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
  ctx.save();
  ctx.translate(cx, cy); ctx.rotate(-heading * Math.PI / 180);
  ctx.font = `${11 * dpr}px ui-monospace, monospace`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (let d = 0; d < 360; d += 30) {
    ctx.save(); ctx.rotate(d * Math.PI / 180);
    if (d % 90 === 0) {
      ctx.fillStyle = d === 0 ? '#f38ba8' : '#a9b1d6';
      ctx.fillText('北东南西'[d / 90], 0, -r * 0.78);
    } else {
      ctx.strokeStyle = '#4f5678'; ctx.lineWidth = dpr;
      ctx.beginPath(); ctx.moveTo(0, -r * 0.92); ctx.lineTo(0, -r * 0.8); ctx.stroke();
    }
    ctx.restore();
  }
  ctx.restore();
  // fixed lubber line + heading readout
  ctx.strokeStyle = '#f9e2af'; ctx.lineWidth = 2 * dpr;
  ctx.beginPath(); ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy - r * 0.62); ctx.stroke();
  ctx.fillStyle = '#cdd6f4';
  ctx.font = `${13 * dpr}px ui-monospace, monospace`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(heading.toFixed(0) + '°', cx, cy);
}

// centered-zero horizontal bar: |label  ────█────  value|
function imuBar(ctx, x, y, w, h, label, val, range, unit, color, dpr) {
  ctx.font = `${11 * dpr}px ui-monospace, monospace`;
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';  ctx.fillStyle = '#6b7394';
  ctx.fillText(label, x, y + h / 2);
  const bx = x + 26 * dpr, bw = w - 92 * dpr, mid = bx + bw / 2;
  ctx.fillStyle = '#1c2030'; ctx.fillRect(bx, y, bw, h);
  const frac = Math.max(-1, Math.min(1, val / range));
  ctx.fillStyle = color;
  if (frac >= 0) ctx.fillRect(mid, y, frac * bw / 2, h);
  else ctx.fillRect(mid + frac * bw / 2, y, -frac * bw / 2, h);
  ctx.fillStyle = '#3d4460'; ctx.fillRect(mid - dpr / 2, y - dpr, dpr, h + 2 * dpr);
  ctx.textAlign = 'right'; ctx.fillStyle = '#a9b1d6';
  ctx.fillText(val.toFixed(1) + unit, x + w, y + h / 2);
}

function paintImu(msg) {
  markFresh(feed.imu);
  const cv = $('imuCanvas');
  if (!cv || !msg || !msg.orientation) return;
  $('imuPh').style.display = 'none';

  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth * dpr, h = cv.clientHeight * dpr;
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, w, h);

  // 4:3 wide canvas, laid out below the overlay row (badge + meta):
  // two dials up top, gyro / accel bars in two columns, one baro line bottom
  const e = eulerOf(msg.orientation);
  const r = w * 0.155;
  const dy = h * 0.34;
  dialHorizon(ctx, w * 0.27, dy, r, e.roll, e.pitch, dpr);
  dialCompass(ctx, w * 0.73, dy, r, e.yaw, dpr);

  ctx.fillStyle = '#6b7394';
  ctx.font = `${11 * dpr}px ui-monospace, monospace`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(`横滚 ${(e.roll * 180 / Math.PI).toFixed(1)}°  俯仰 ${(e.pitch * 180 / Math.PI).toFixed(1)}°`,
               w * 0.27, dy + r + 12 * dpr);
  ctx.fillText('航向 Yaw', w * 0.73, dy + r + 12 * dpr);

  const g = msg.angular_velocity, a = msg.linear_acceleration;
  const deg = v => v * 180 / Math.PI, gee = v => v / 9.80665;
  const bh = 9 * dpr, colw = w * 0.44;
  const cols = [
    [w * 0.04, [['ωX', deg(g.x), 300, '°', '#89b4fa'], ['ωY', deg(g.y), 300, '°', '#89b4fa'],
                ['ωZ', deg(g.z), 300, '°', '#89b4fa']]],
    [w * 0.52, [['aX', gee(a.x), 2, 'g', '#a6e3a1'], ['aY', gee(a.y), 2, 'g', '#a6e3a1'],
                ['aZ', gee(a.z), 2, 'g', '#a6e3a1']]],
  ];
  for (const [cx0, rows] of cols) {
    let y = h * 0.68;
    for (const [lb, v, rg, un, c] of rows) {
      imuBar(ctx, cx0, y, colw, bh, lb, v, rg, un, c, dpr);
      y += h * 0.095;
    }
  }

  // bottom line: mag raw + baro (alt derived from pressure, ISA formula)
  const st = imuState;
  const mg = st.mag ? `磁 ${st.mag.x.toFixed(0)}/${st.mag.y.toFixed(0)}/${st.mag.z.toFixed(0)}` : '磁 —';
  const tp = st.temp != null ? `${st.temp.toFixed(1)}°C` : '—';
  const pr = st.press != null ? `${(st.press / 100).toFixed(1)}hPa` : '—';
  const alt = st.press != null
    ? `高度 ${(44330 * (1 - Math.pow(st.press / 101325, 0.1903))).toFixed(1)}m` : '';
  ctx.fillStyle = '#6b7394';
  ctx.font = `${11 * dpr}px ui-monospace, monospace`;
  ctx.textAlign = 'center';
  ctx.fillText(`${mg}   ${tp}  ${pr}  ${alt}`, w / 2, h * 0.955);

  $('imuMeta').textContent = `${srcLabel('imu')}预览 ${feed.imu.hz.toFixed(1)} Hz`;
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
  // 源 = board node's self-reported publish rate (/diagnostics, real);
  // 预览 = throttled rosbridge delivery rate here (~10 fps by IMG_THROTTLE_MS)
  $(f.key + 'Meta').textContent =
    `${srcLabel(f.key)}预览 ${feed[f.key].hz.toFixed(1)} fps`;
}

// ---- freshness badges (one authority, ticked at 2 Hz) --------------------

function badge(id, text, cls) {
  const el = $(id);
  if (el) { el.textContent = text; el.className = 'vbadge ' + cls; }
}

function tickAge() {
  const connected = ws && ws.readyState === WebSocket.OPEN;
  const rows = [['scan', 'scanBadge', '/scan'],
                ['imu', 'imuBadge', 'IMU'],
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
  feed.imu.at = 0; feed.imu.hz = 0;
  imuState.mag = imuState.temp = imuState.press = null;
  $('imuPh').style.display = '';
  $('imuMeta').textContent = '—';
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
for (const id of ['scanTopic', 'imuTopic', 'depthTopic', 'frontTopic', 'wristTopic']) {
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
