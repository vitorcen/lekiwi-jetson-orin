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
//    NOT auto-promote. Leaving the tab or hiding the page forces "idle" AND
//    flips the button back to stopped. While 解读中 we poll /caption
//    at 1 Hz; each caption/answer carries a thumbnail of the exact interpreted
//    frame, rendered beside the text. The one-shot ask box stays always usable.
//
// Defensive but not twitchy: 离线 needs HEALTH_FAILS consecutive failed health
// polls, not one — a lone timeout or a daemon restart shows 重连中… and rides
// through. Once offline we probe at 0.5 Hz and resume automatically. A
// deliberate stand-down (page hidden, 断开) reads 已暂停/已断开, never 离线:
// that distinction is what tells you whether to go debug the board.
import { $, S, invoke } from './state.js';

const FRAME_MIN_MS = 33;   // cap the frame pump at ~30 fps (native-ish)
const CAPTION_MS = 1000;   // 1 Hz caption pull, only while 解读中
const HEALTH_MS  = 1000;   // 1 Hz health while online
const PROBE_MS   = 2000;   // 0.5 Hz health while offline (reconnect probe)
const HEALTH_FAILS = 3;    // consecutive failed polls before declaring 离线

let wantActive = true;     // user intent: false only after an explicit 断开
let active     = false;    // tab is the visible foreground AND wantActive
let online     = false;    // daemon is answering
let watching   = false;    // 解读中: GPU captioning between start/stop
let lastSeq    = -1;       // dedupe repeated captions
let lastFrameAt = 0;       // client clock of the last decoded frame (for age)
let curFps     = 0;        // daemon-measured capture fps (X-Fps)
let framePumping = false;  // a frame pump loop is running (never stack two)
let healthErrs = 0;        // consecutive failed health polls
let lastHealthErr = '';    // why the last poll failed (shown on the 离线 badge)

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
  // The daemon is the authority on the cadence (it clamps); mirror it back
  // unless the user is mid-edit in that field.
  const iv = $('vlmInterval');
  if (h && h.watch_interval != null && document.activeElement !== iv)
    iv.value = String(h.watch_interval);
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

// ---- VLM model switch ----------------------------------------------------

let switchingModel = false;   // a model switch is mid-flight (poll, don't repaint)

// Populate the dropdown from GET /models (via the vlm daemon). Disabled options
// for models without a paired mmproj; the active one is selected. Skipped while
// a switch is in flight so the poller owns the <select> then.
async function loadModels() {
  const sel = $('vlmModel');
  if (!sel || !invoke || switchingModel) return;
  try {
    const r = JSON.parse(await invoke('vlm_models', { ip: curIp() }));
    const models = r.models || [];
    sel.innerHTML = '';
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m.id;
      const mb = m.disk_mb != null ? ` (${m.disk_mb}MB)` : '';
      opt.textContent = m.id + mb + (m.usable ? '' : ' · 缺 mmproj');
      opt.disabled = !m.usable;
      opt.selected = !!m.active;
      sel.append(opt);
    }
    sel.disabled = !online || models.length === 0;
  } catch { /* offline / transient — leave the dropdown as-is */ }
}

// Change -> switch via the voice-daemon /config vision job (it drives the vlm
// daemon POST /model and forwards progress to its feed). We don't subscribe to
// that feed here; instead we poll /models until active flips or we time out.
$('vlmModel') && ($('vlmModel').onchange = async () => {
  const sel = $('vlmModel');
  const id = sel.value;
  if (!invoke || !id) return;
  switchingModel = true;
  sel.disabled = true;
  addCaption(`切换视觉模型到 ${id}…（冷加载需时,请稍候）`, null, 'ask');
  try {
    // voice-daemon proxy (port 8092 on the same board); returns 202 + job_id.
    await invoke('voice_post', {
      ip: curIp(), path: '/config',
      body: JSON.stringify({ axis: 'vision', value: { model: id } }),
    });
  } catch (e) {
    addCaption('切换请求失败: ' + e, null, 'error');
    switchingModel = false; sel.disabled = false; loadModels(); return;
  }
  const deadline = Date.now() + 150000;   // > llama cold-load ceiling
  let done = false;
  while (Date.now() < deadline) {
    await sleep(3000);
    try {
      const r = JSON.parse(await invoke('vlm_models', { ip: curIp() }));
      const act = (r.models || []).find(m => m.active);
      if (act) {
        if (act.id === id) { addCaption(`已切换到 ${id}`, null, 'answer'); done = true; break; }
        if (!r.busy) { addCaption(`切换未生效,仍为 ${act.id}（可能已还原,见语音日志）`, null, 'error'); done = true; break; }
      }
    } catch { /* daemon restarting llama — keep polling */ }
  }
  if (!done) addCaption('切换超时,请检查板端 llama-server', null, 'error');
  switchingModel = false;
  loadModels();
});

// ---- polling loops -------------------------------------------------------

async function pollHealth() {
  if (!active || !invoke) return;
  try {
    const h = JSON.parse(await invoke('vlm_health', { ip: curIp() }));
    healthErrs = 0;
    if (!online) goOnline();
    else framePump();          // 兜底:在线但泵已死(有 framePumping 防重入)
    paintHealth(h);
  } catch (e) {
    // Tolerate a hiccup. One failed poll used to drop us straight to 离线,
    // which the frame pump right below explicitly refuses to do for the same
    // class of error (daemon restarting, a single timeout). Only a sustained
    // outage — HEALTH_FAILS consecutive misses — counts as offline.
    healthErrs++;
    lastHealthErr = String(e);
    if (online) {
      if (healthErrs >= HEALTH_FAILS) goOffline();
      else setBadge('重连中…', 'warn');
    } else {
      setBadge('离线', 'bad');   // stay offline; probe keeps running
    }
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
        // 瞬时失败(相机抖动/daemon 正在重启)绝不能杀泵:health 探测在线时
        // 状态永不翻转,泵一死画面就永久定格。退避 1s 继续,离线由外层条件退出。
        await sleep(1000);
        continue;
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
  loadModels();  // fill the model dropdown once the daemon answers
}

function goOffline() {
  online = false;
  setWatching(false);        // drop 解读 loop + reset button
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, PROBE_MS);   // slow reconnect probe
  curFps = 0; paintFps();
  setBadge('离线', 'bad');
  // Put the cause where it can be seen: a bare 离线 is unfalsifiable, and this
  // badge is the only place the operator ever looks.
  for (const el of [$('vState'), $('vStateBadge')])
    if (el && lastHealthErr) el.title = '最后一次失败: ' + lastHealthErr;
  metaOffline();
  const btn = $('vlmWatchBtn'); if (btn) btn.disabled = true;
  const sel = $('vlmModel'); if (sel && !switchingModel) sel.disabled = true;
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
  healthErrs = 0;
  if (!ageTimer) ageTimer = setInterval(tickAge, 500);
  healthTimer = setInterval(pollHealth, PROBE_MS);   // probe until first success
  setBadge('连接中…', 'warn');
  pollHealth();
}

// Tab leave / page hidden / 断开: stand down. Force the daemon to idle (stop
// GPU) and flip the 解读 button back to stopped.
function stopActive() {
  if (!active) return;
  active = false;
  if (watching && invoke) invoke('vlm_set_state', { ip: curIp(), state: 'idle' }).catch(() => {});
  setWatching(false);
  clearTimers();
  if (ageTimer) { clearInterval(ageTimer); ageTimer = null; }
  online = false;
  healthErrs = 0;
  curFps = 0; paintFps();
  // We stood the feed down on purpose (hidden page / 断开 button) — the daemon
  // is fine. Calling that 离线 is a lie that sent people debugging the board.
  setBadge(wantActive ? '已暂停' : '已断开', 'warn');
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
  invoke('vlm_set_state', {
    ip: curIp(), state: want ? 'watch' : 'idle', interval: curInterval(),
  }).catch(() => {});
  setWatching(want);
};

// 解读周期 (s). The daemon owns the real value and clamps it; this input is
// pushed on change and on every 开始解读, and reconciled from /health.
function curInterval() {
  const v = parseFloat($('vlmInterval').value);
  return Number.isFinite(v) ? v : 10;
}
$('vlmInterval').addEventListener('change', () => {
  if (!invoke) return;
  // interval only — retunes a running watch loop without stopping it.
  invoke('vlm_set_state', { ip: curIp(), interval: curInterval() }).catch(() => {});
});

// 清空: local display only. The daemon's caption ring is untouched, so lastSeq
// must stay as-is or the next poll would re-add the caption we just cleared.
$('capClear').onclick = () => { $('capfeed').innerHTML = ''; };

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

// Stand down only when the page is actually HIDDEN (minimised / other tab).
// NOT on window blur: clicking another app leaves the console fully visible,
// and tearing the feed down there made Vision look like it dropped offline
// every time focus moved. Voice and ROS have always behaved this way; Vision
// inherited the blur rule from the ZMQ tab, where it is a safety requirement
// (never drive a robot nobody is watching) that simply does not apply to a
// read-only camera view.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopActive();
  else if (S.page === 'vision') startActive();
});
