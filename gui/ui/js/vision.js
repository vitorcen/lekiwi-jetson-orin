// Vision tab: camera stream + VLM captions from the vlm-daemon HTTP API.
//
// The Rust backend owns all HTTP and the bearer token (the WebView never sees
// it), so here we only poll Tauri commands and paint. Two independent concerns:
//
//  - CAMERA VIEW (CPU only): runs whenever the tab is the visible foreground.
//    We pump frames as fast as the backend returns them, capped at ~30 fps
//    (chain the next request on completion, never stack), and show the daemon's
//    measured fps (X-Fps header, forwarded by the Rust vlm_frame command). Frame
//    polling never triggers GPU inference on the daemon.
//
//  - GPU CAPTIONING (解读): runs ONLY between an explicit 开始解读 press
//    (vlm_set_state "watch") and 停止解读 (vlm_set_state "idle"). Tab enter does
//    NOT auto-promote. Leaving / blurring / hiding the tab forces "idle" AND
//    flips the button back to stopped (dead-man). While 解读中 we poll /caption
//    at 1 Hz; each caption/answer carries a thumbnail of the exact interpreted
//    frame, rendered beside the text. The one-shot ask box stays always usable.
//
// Defensive: any command error means the daemon is unreachable — we flip to the
// 离线 state, stop the feeds, and keep probing health at 0.5 Hz until it answers
// again, then resume automatically.
import { $, S, invoke } from './state.js';

const FRAME_MIN_MS = 33;   // cap the frame pump at ~30 fps (native-ish)
const CAPTION_MS = 1000;   // 1 Hz caption pull, only while 解读中
const HEALTH_MS  = 1000;   // 1 Hz health while online
const PROBE_MS   = 2000;   // 0.5 Hz health while offline (reconnect probe)

let wantActive = true;     // user intent: false only after an explicit 断开
let active     = false;    // tab is the visible foreground AND wantActive
let online     = false;    // daemon is answering
let watching   = false;    // 解读中: GPU captioning between start/stop
let lastSeq    = -1;       // dedupe repeated captions
let lastFrameAt = 0;       // client clock of the last decoded frame (for age)
let curFps     = 0;        // daemon-measured capture fps (X-Fps)
let framePumping = false;  // a frame pump loop is running (never stack two)

let healthTimer = null, capTimer = null, ageTimer = null;

function curIp() { return ($('vip') && $('vip').value.trim()) || '127.0.0.1'; }
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- badges / meta -------------------------------------------------------

function setBadge(text, cls) {
  for (const el of [$('vState'), $('vStateBadge')]) {
    if (!el) continue;
    el.textContent = text;
    el.className = (el.id === 'vStateBadge' ? 'vbadge ' : 'pill ') + cls;
  }
}

// One authority for the state badge: 离线 / 解读中 / 画面中 / 空闲.
function paintStateBadge() {
  if (!online) { setBadge('离线', 'bad'); return; }
  if (watching) { setBadge('解读中 watch', 'ok'); return; }
  if (curFps > 0) { setBadge('画面中 live', 'info'); return; }
  setBadge('空闲 idle', 'warn');
}

function paintFps() {
  const el = $('vFps');
  if (!el) return;
  el.textContent = (online && curFps > 0) ? curFps.toFixed(1) + ' fps' : '— fps';
}

function paintHealth(h) {
  // Reconcile with the daemon: if it auto-demoted (safety net) while we thought
  // we were 解读中, reflect that and flip the button back.
  if (watching && h && h.state !== 'watch') setWatching(false);
  $('vLlama').textContent = 'llama ' + (h && h.llama_up ? '✓' : '✕');
  $('vLlama').style.color = h && h.llama_up ? '#7ee2a8' : '#f38ba8';
  $('vCam').textContent = '相机 ' + (h && h.camera ? '✓' : '✕');
  $('vCam').style.color = h && h.camera ? '#7ee2a8' : '#f38ba8';
  if (h && h.uptime != null) {
    const s = Math.round(+h.uptime);
    $('vUptime').textContent = '运行 ' + (s >= 3600
      ? Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm'
      : s >= 60 ? Math.floor(s / 60) + 'm' + (s % 60) + 's' : s + 's');
  }
  paintStateBadge();
}

function metaOffline() {
  for (const [id, t] of [['vLlama', 'llama —'], ['vCam', '相机 —'], ['vUptime', '运行 —']]) {
    $(id).textContent = t; $(id).style.color = '#6b7394';
  }
}

// Frame freshness on the client clock (server frame_ts needs clock sync to be
// meaningful): time since the last frame actually decoded here.
function tickAge() {
  const age = $('vFrameAge');
  if (!age) return;
  if (!online || !lastFrameAt) { age.textContent = '—'; age.className = 'vage'; return; }
  const d = (Date.now() - lastFrameAt) / 1000;
  age.textContent = d < 1 ? '实时' : '帧龄 ' + d.toFixed(1) + 's';
  age.className = 'vage' + (d > 2 ? ' stale' : '');
}

// ---- caption feed --------------------------------------------------------

function pad2(n) { return String(n).padStart(2, '0'); }

// kind: '' (live caption) | 'ask' (question echo) | 'answer' | 'error'
// frameB64: thumbnail of the EXACT interpreted frame (may be null).
function addCaption(text, latencyMs, kind, frameB64) {
  const feed = $('capfeed');
  if (!feed) return;
  const t = new Date();
  const row = document.createElement('div');
  row.className = 'caprow' + (kind ? ' cap-' + kind : '');
  if (frameB64) {
    const img = document.createElement('img');
    img.className = 'capthumb';
    img.alt = 'interpreted frame';
    img.src = 'data:image/jpeg;base64,' + frameB64;
    row.append(img);
  }
  const textwrap = document.createElement('div');
  textwrap.className = 'captext';
  const meta = document.createElement('div');
  meta.className = 'capmeta';
  const ts = `${pad2(t.getHours())}:${pad2(t.getMinutes())}:${pad2(t.getSeconds())}`;
  meta.textContent = latencyMs != null ? `${ts} · ${Math.round(latencyMs)}ms` : ts;
  const msg = document.createElement('div');
  msg.className = 'capmsg';
  msg.textContent = text;   // textContent: model output is untrusted
  textwrap.append(meta, msg);
  row.append(textwrap);
  feed.insertBefore(row, feed.firstChild);   // newest on top
  while (feed.childElementCount > 200) feed.removeChild(feed.lastChild);
}

// ---- polling loops -------------------------------------------------------

async function pollHealth() {
  if (!active || !invoke) return;
  try {
    const h = JSON.parse(await invoke('vlm_health', { ip: curIp() }));
    if (!online) goOnline();
    paintHealth(h);
  } catch {
    if (online) goOffline();
    else setBadge('离线', 'bad');   // stay offline; probe keeps running
  }
}

// Self-driving frame pump: request a frame, paint it, then chain the next
// request after a small delay so we approach the native rate without stacking.
async function framePump() {
  if (framePumping) return;
  framePumping = true;
  try {
    while (active && online && invoke) {
      const t0 = performance.now();
      try {
        const r = JSON.parse(await invoke('vlm_frame', { ip: curIp() }));
        $('vlmImg').src = 'data:image/jpeg;base64,' + r.b64;
        $('vlmImg').classList.add('live');
        $('vidplaceholder').style.display = 'none';
        lastFrameAt = Date.now();
        curFps = +r.fps || 0;
        paintFps();
        paintStateBadge();
        tickAge();
      } catch {
        break;   // transient/offline; health loop owns online/offline
      }
      const dt = performance.now() - t0;
      if (dt < FRAME_MIN_MS) await sleep(FRAME_MIN_MS - dt);
    }
  } finally {
    framePumping = false;
  }
}

async function pollCaption() {
  if (!active || !online || !watching || !invoke) return;
  try {
    const c = JSON.parse(await invoke('vlm_caption', { ip: curIp() }));
    if (c && c.seq !== undefined && c.seq === lastSeq) return;   // nothing new
    if (c) { lastSeq = c.seq; if (c.text) addCaption(c.text, c.latency_ms, '', c.frame_b64); }
  } catch { /* transient */ }
}

// ---- 解读 (GPU captioning) start/stop -----------------------------------

// Manage the 解读中 state + its 1 Hz caption loop + button UI. Does NOT itself
// POST /state — callers (button click, dead-man) decide whether to notify the
// daemon; this only reconciles local UI/timers.
function setWatching(on) {
  watching = on && online;
  const btn = $('vlmWatchBtn');
  if (btn) {
    btn.textContent = watching ? '停止解读' : '开始解读';
    btn.classList.toggle('live', watching);
    btn.disabled = !online;
  }
  if (watching) {
    if (!capTimer) capTimer = setInterval(pollCaption, CAPTION_MS);
    pollCaption();
  } else if (capTimer) {
    clearInterval(capTimer); capTimer = null;
  }
  paintStateBadge();
}

function goOnline() {
  online = true;
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, HEALTH_MS);
  const btn = $('vlmWatchBtn'); if (btn) btn.disabled = false;
  framePump();   // start the camera feed (CPU only)
}

function goOffline() {
  online = false;
  setWatching(false);        // drop 解读 loop + reset button
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, PROBE_MS);   // slow reconnect probe
  curFps = 0; paintFps();
  setBadge('离线', 'bad');
  metaOffline();
  const btn = $('vlmWatchBtn'); if (btn) btn.disabled = true;
  $('vlmImg').classList.remove('live');
  $('vidplaceholder').style.display = '';
}

function clearTimers() {
  for (const t of [healthTimer, capTimer]) if (t) clearInterval(t);
  healthTimer = capTimer = null;
}

// ---- activation (mirrors the ZMQ dead-man pattern) -----------------------

// Tab enter: start the CAMERA feed only. NO auto-promote to watch (GPU).
function startActive() {
  if (active || !wantActive || S.page !== 'vision' || document.hidden) return;
  active = true;
  online = false;
  if (!ageTimer) ageTimer = setInterval(tickAge, 500);
  healthTimer = setInterval(pollHealth, PROBE_MS);   // probe until first success
  setBadge('连接中…', 'warn');
  pollHealth();
}

// Tab leave / blur / hide: dead-man. Force the daemon to idle (stop GPU) and
// flip the button back to stopped.
function stopActive() {
  if (!active) return;
  active = false;
  if (watching && invoke) invoke('vlm_set_state', { ip: curIp(), state: 'idle' }).catch(() => {});
  setWatching(false);
  clearTimers();
  if (ageTimer) { clearInterval(ageTimer); ageTimer = null; }
  online = false;
  curFps = 0; paintFps();
  setBadge('离线', 'bad');
  metaOffline();
}

// ---- exported hooks for tab switching (main.js) --------------------------

export function onEnterVision() { startActive(); refreshSvcAuto(); }
export function onLeaveVision() { stopActive(); }

// ---- wiring --------------------------------------------------------------

// Manual service control: runtime only, never touches boot autostart.
for (const [id, action, label] of [
  ['vsvcRestart', 'restart', '重启'],
  ['vsvcStop', 'stop', '停止'],
  ['vsvcStart', 'start', '启动'],
]) {
  const btn = $(id);
  if (!btn) continue;
  btn.onclick = async () => {
    if (!invoke) return;
    btn.disabled = true;
    addCaption(`${label}视觉服务…`, null, 'ask');
    try {
      await invoke('vlm_service', { ip: curIp(), action });
      addCaption(`服务${label}完成`, null, 'ask');
    } catch (e) {
      addCaption(`服务${label}失败: ${e}`, null, 'ask');
    } finally {
      btn.disabled = false;   // health poll flips online/offline on its own
    }
  };
}

// Boot-autostart checkbox: mirrors `systemctl --user is-enabled`, toggles
// enable/disable. Independent of the runtime start/stop buttons above.
async function refreshSvcAuto() {
  const cb = $('vsvcAuto');
  if (!cb || !invoke) return;
  try {
    const out = await invoke('vlm_service', { ip: curIp(), action: 'is-enabled' });
    cb.checked = out.split('\n').some(l => l.trim() === 'enabled');
    cb.disabled = false;
  } catch { cb.disabled = true; }
}
$('vsvcAuto') && ($('vsvcAuto').onchange = async e => {
  const cb = e.target;
  cb.disabled = true;
  try {
    await invoke('vlm_service', { ip: curIp(), action: cb.checked ? 'enable' : 'disable' });
    addCaption(`开机自启已${cb.checked ? '开启' : '关闭'}`, null, 'ask');
  } catch (err) {
    addCaption(`开机自启设置失败: ${err}`, null, 'ask');
    cb.checked = !cb.checked;
  } finally {
    cb.disabled = false;
  }
});

// Button always shows the ACTION it will perform (auto-connected -> 断开).
$('vconnBtn').onclick = () => {
  wantActive = !wantActive;
  $('vconnBtn').textContent = wantActive ? '断开' : '连接';
  $('vconnBtn').classList.toggle('live', !wantActive);
  if (wantActive) startActive();
  else stopActive();
};

// 开始解读 / 停止解读: the only normal promoter of GPU captioning.
$('vlmWatchBtn').onclick = () => {
  if (!invoke || !online) return;
  const want = !watching;
  invoke('vlm_set_state', { ip: curIp(), state: want ? 'watch' : 'idle' }).catch(() => {});
  setWatching(want);
};

$('vlmAskBtn').onclick = async () => {
  if (!invoke) return;
  const q = $('vlmAsk').value.trim();
  addCaption(q || '（默认描述）', null, 'ask');
  $('vlmAsk').value = '';
  const btn = $('vlmAskBtn');
  btn.disabled = true;
  try {
    const r = JSON.parse(await invoke('vlm_describe', { ip: curIp(), prompt: q }));
    addCaption(r.text || '(空)', r.latency_ms, 'answer', r.frame_b64);
  } catch (e) {
    addCaption('请求失败: ' + e, null, 'error');
  } finally {
    btn.disabled = false;
  }
};
$('vlmAsk').addEventListener('keydown', e => { if (e.key === 'Enter') $('vlmAskBtn').click(); });

// Self-contained dead-man: window blur / page hidden both stand the daemon down,
// visibility returning re-arms the CAMERA feed (only if on Vision tab + wanted).
window.addEventListener('blur', () => { if (S.page === 'vision') stopActive(); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopActive();
  else if (S.page === 'vision') startActive();
});
