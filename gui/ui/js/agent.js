// Agent tab (formerly the voice tab): Hermes real-time voice chat via the
// voice-daemon HTTP API. Page id is #page-agent, data-page="agent".
//
// Same trust boundary as vision.js: the Rust backend owns the bearer token,
// we only poll Tauri commands (voice_get/voice_post) and paint. The daemon
// keeps a 200-event ring; we pull increments with GET /feed?since=<seq> at
// ~2.5 Hz while the tab is active, so transcript survives brief GUI absence.
//
// Event types from the daemon feed:
//   state           {state: idle|listening|thinking|speaking, window_deadline}
//   user_text       {text}        — a finalized ASR utterance
//   assistant_delta {delta}       — streamed LLM tokens, appended to one bubble
//   tool            {tool_name}   — Hermes tool call (e.g. vlm_look)
//   tts             {sentence, backend}
//   error           {message}
import { $, S, invoke } from './state.js';

const HEALTH_MS = 1000;
const PROBE_MS  = 2500;
const FEED_MS   = 400;

let active = false, online = false;
let healthTimer = null, feedTimer = null;
let lastSeq = 0;
let vstate = 'idle';        // daemon state machine mirror
let deadline = 0;           // 常开窗口 server-side deadline (epoch s), 0 = none
let curAnswer = null;       // the assistant bubble currently receiving deltas

// brain strip state
let brainSwitching = false; // a /brain job is in flight (select frozen)
let brainJob = null;        // current job_id we are tracking
let brainPreset = null;     // last-known selected preset (to revert on failure)

function curIp() { return ($('voip') && $('voip').value.trim()) || '127.0.0.1'; }

// ---- feed rendering ------------------------------------------------------

function pad2(n) { return String(n).padStart(2, '0'); }

function addRow(text, kind, brain) {
  const feed = $('vofeed');
  if (!feed) return null;
  const t = new Date();
  const row = document.createElement('div');
  row.className = 'caprow' + (kind ? ' cap-' + kind : '');
  const textwrap = document.createElement('div');
  textwrap.className = 'captext';
  const meta = document.createElement('div');
  meta.className = 'capmeta';
  meta.textContent = `${pad2(t.getHours())}:${pad2(t.getMinutes())}:${pad2(t.getSeconds())}`;
  if (brain) {   // 角标:哪个大脑答的话(feed 事件带 brain 字段时)
    const badge = document.createElement('span');
    badge.className = 'brainbadge';
    badge.textContent = brain;   // untrusted; textContent only
    meta.append(' ', badge);
  }
  const msg = document.createElement('div');
  msg.className = 'capmsg';
  msg.textContent = text;   // untrusted model/ASR output
  textwrap.append(meta, msg);
  row.append(textwrap);
  feed.insertBefore(row, feed.firstChild);   // newest on top
  while (feed.childElementCount > 200) feed.removeChild(feed.lastChild);
  return msg;
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'state':
      vstate = ev.state || 'idle';
      deadline = +ev.window_deadline || 0;
      paintState();
      if (vstate === 'idle') curAnswer = null;
      break;
    case 'user_text':
      curAnswer = null;                       // next deltas start a new bubble
      addRow('🗣 ' + ev.text, 'ask');
      break;
    case 'assistant_delta':
      if (!curAnswer) curAnswer = addRow('', 'answer', ev.brain);
      if (curAnswer) curAnswer.textContent += ev.delta || '';
      break;
    case 'tool':
      addRow('🔧 调用 ' + (ev.tool_name || '?') + ' …', 'ask');
      break;
    case 'tts':
      if (ev.backend && ev.backend !== 'edge') {
        const st = $('aStage');
        if (st) st.textContent = '本地音色播报(云端 TTS 降级)';
      }
      break;
    case 'error':
      addRow('⚠ ' + (ev.message || '出错'), 'error');
      break;
    case 'audio':                     // 设备缺失的对称提示:恢复也要看得见
      addRow('✓ ' + (ev.message || '音频设备已恢复'), 'sys');
      break;
    case 'barge_in':
      addRow('✋ 打断' + (ev.action === 'stop' ? '(停止)' : '') + ': ' + (ev.text || ''), 'sys');
      break;
    case 'drift':
      if (ev.axis === 'brain') addRow('⚠ ' + (ev.message || '大脑配置漂移'), 'error');
      break;
    case 'job':
      if (ev.axis === 'brain') handleBrainJob(ev);
      break;
  }
}

// ---- brain switch job tracking ------------------------------------------
const BRAIN_PHASE_TXT = {
  start: '开始切换…', precheck: '前置校验…', patch: '下发补丁…',
  restart: '重启网关…', probe: '探针验证…',
};

function setBrainStatus(txt, cls) {
  const el = $('aBrainStatus');
  if (!el) return;
  el.textContent = txt || '';
  el.className = 'bbstat' + (cls ? ' ' + cls : '');
  el.style.display = txt ? '' : 'none';
}

function handleBrainJob(ev) {
  if (ev.phase in BRAIN_PHASE_TXT) {
    brainSwitching = true;
    const sel = $('aBrainSel');
    if (sel) sel.disabled = true;
    setBrainStatus('⏳ ' + BRAIN_PHASE_TXT[ev.phase], 'info');
    return;
  }
  if (ev.phase === 'done') {
    brainSwitching = false; brainJob = null;
    setBrainStatus('✓ 已切换到 ' + (ev.preset || ''), 'ok');
    addRow('🧠 大脑已切换到 ' + (ev.preset || ''), 'sys');
    setTimeout(() => setBrainStatus(''), 4000);
    refreshBrain();
  } else if (ev.phase === 'reverted') {
    brainSwitching = false; brainJob = null;
    const reason = ev.reason || '未知原因';
    setBrainStatus('✗ 切换失败,已还原', 'bad');
    addRow('⚠ 大脑切换失败已还原:' + reason
      + (ev.old_probe ? '(旧模型探针 ' + ev.old_probe + ')' : ''), 'error');
    setTimeout(() => setBrainStatus(''), 8000);
    refreshBrain();   // snaps the dropdown back to the reverted preset
  }
}

// ---- state painting ------------------------------------------------------

const STATE_TXT = {
  idle:      ['待机 idle', 'warn'],
  listening: ['聆听中 listening', 'ok'],
  thinking:  ['思考中 thinking', 'info'],
  speaking:  ['播报中 speaking', 'info'],
};

function paintState() {
  const pill = $('aState');
  if (!online) {
    if (pill) { pill.textContent = '离线'; pill.className = 'pill bad'; }
  } else {
    const [txt, cls] = STATE_TXT[vstate] || STATE_TXT.idle;
    if (pill) { pill.textContent = txt; pill.className = 'pill ' + cls; }
  }
  const lbtn = $('aListenBtn');
  if (lbtn) {
    lbtn.disabled = !online;
    lbtn.textContent = (online && vstate !== 'idle') ? '结束对话' : '开始对话';
    lbtn.classList.toggle('live', online && vstate !== 'idle');
  }
  const ibtn = $('aIntBtn');
  if (ibtn) ibtn.disabled = !(online && (vstate === 'thinking' || vstate === 'speaking'));
  const st = $('aStage');
  if (st && online) {
    if (vstate === 'idle') st.textContent = '待机(麦克风关闭)';
    else if (deadline > 0) {
      const left = Math.max(0, Math.round(deadline - Date.now() / 1000));
      st.textContent = (STATE_TXT[vstate] || [''])[0].split(' ')[0]
        + ` · 常开剩 ${Math.floor(left / 60)}分${pad2(left % 60)}秒`;
    }
  } else if (st && !online) st.textContent = '服务离线';
}

// ---- brain strip (read-only this phase; switching is P2) ------------------
// Populated from GET /config. The endpoint may not exist yet (added by the
// daemon P0b work) — any failure just leaves the strip in its neutral state.

function ttsDesc(tts) {
  if (!tts) return '—';
  if (typeof tts === 'string') return tts;
  return tts.engine + (tts.voice ? '(' + tts.voice + ')' : '');
}

function renderBrain(cfg) {
  const desired = (cfg && cfg.desired) || cfg || {};
  const presets = desired.presets || {};
  const brain = desired.brain || {};
  const caps = (cfg && cfg.capabilities) || [];
  brainPreset = brain.preset || null;
  const sel = $('aBrainSel');
  if (sel) {
    sel.innerHTML = '';
    for (const name of Object.keys(presets)) {
      const o = document.createElement('option');
      o.value = name;
      o.textContent = name;   // untrusted config; textContent only
      if (name === brain.preset) o.selected = true;
      sel.append(o);
    }
    if (!Object.keys(presets).length) {
      const o = document.createElement('option');
      o.textContent = brain.preset || '—';
      sel.append(o);
    }
    // frozen while a switch is running or the daemon is offline
    sel.disabled = !online || brainSwitching;
  }
  // capability badges from the profile (mcp_servers keys) — same for every cloud
  // preset since capability comes from the profile, not the model (🛞 = drive).
  const capEl = $('aBrainCap');
  if (capEl) {
    const has = caps.length > 0;
    capEl.textContent = has
      ? (caps.includes('drive') ? '🛞 ' : '') + caps.join(' · ')
      : '';
    capEl.style.display = has ? '' : 'none';
  }
  const pairEl = $('aBrainPair');
  if (pairEl) {
    const cur = presets[brain.preset];
    const pair = cur && cur.pair;
    pairEl.textContent = pair
      ? `搭配 ASR ${pair.asr || '—'} · TTS ${ttsDesc(pair.tts)}`
      : '搭配 —';
  }
  const drift = $('aBrainDrift');
  if (drift) {
    const d = cfg && cfg.drift;
    const has = d && (Array.isArray(d) ? d.length : Object.keys(d).length);
    drift.style.display = has ? '' : 'none';
  }
}

async function refreshBrain() {
  if (!invoke) return;
  try {
    renderBrain(JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/config' })));
  } catch { /* /config not up yet — leave the strip neutral */ }
}

// ---- polling -------------------------------------------------------------

async function pollHealth() {
  if (!active || !invoke) return;
  try {
    const h = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/health' }));
    vstate = h.state || 'idle';
    deadline = +h.window_deadline || 0;
    if (!online) goOnline();
    paintState();
  } catch {
    if (online) goOffline();
    else paintState();
  }
}

async function pollFeed() {
  if (!active || !online || !invoke) return;
  try {
    const r = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/feed?since=' + lastSeq }));
    // Daemon restart resets seq; detect and re-pull from scratch.
    if (r.last_seq < lastSeq) { lastSeq = 0; return; }
    for (const ev of r.events || []) handleEvent(ev);
    lastSeq = r.last_seq;
  } catch { /* transient; health loop owns online/offline */ }
}

function goOnline() {
  online = true;
  lastSeq = 0;               // full replay of the ring on (re)connect
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, HEALTH_MS);
  if (!feedTimer) feedTimer = setInterval(pollFeed, FEED_MS);
  refreshSvcAuto();
  refreshBrain();
  paintState();
}

function goOffline() {
  online = false;
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, PROBE_MS);
  if (feedTimer) { clearInterval(feedTimer); feedTimer = null; }
  paintState();
}

// ---- activation ----------------------------------------------------------
// NOTE: no dead-man here, deliberately — the conversation window lives on the
// daemon (it keeps listening/speaking with the GUI closed); leaving the tab
// only stops the polling, never the session.

function startActive() {
  if (active || S.page !== 'agent') return;
  active = true;
  online = false;
  healthTimer = setInterval(pollHealth, PROBE_MS);
  pollHealth();
}

function stopActive() {
  if (!active) return;
  active = false;
  for (const t of [healthTimer, feedTimer]) if (t) clearInterval(t);
  healthTimer = feedTimer = null;
  online = false;
  paintState();
}

export function onEnterAgent() { startActive(); }
export function onLeaveAgent() { stopActive(); }

// ---- wiring --------------------------------------------------------------

$('aListenBtn').onclick = async () => {
  if (!invoke || !online) return;
  try {
    if (vstate === 'idle') {
      const mins = Math.max(1, Math.min(60, +$('aWin').value || 30));
      await invoke('voice_post', {
        ip: curIp(), path: '/listen',
        body: JSON.stringify({ window_s: mins * 60 }),
      });
    } else {
      await invoke('voice_post', { ip: curIp(), path: '/stop', body: '{}' });
    }
    pollHealth();
  } catch (e) { addRow('操作失败: ' + e, 'error'); }
};

$('aIntBtn').onclick = async () => {
  if (!invoke) return;
  try { await invoke('voice_post', { ip: curIp(), path: '/interrupt', body: '{}' }); }
  catch (e) { addRow('打断失败: ' + e, 'error'); }
};

// 发送: text goes through the FULL turn (Hermes → spoken reply), exactly as if
// the user had said it aloud — the daemon's /simulate injects it as ASR output.
// The user_text/assistant_delta/tts events then arrive via the normal feed.
$('aSendBtn').onclick = async () => {
  if (!invoke || !online) return;
  const t = $('aSay').value.trim();
  if (!t) return;
  $('aSay').value = '';
  try { await invoke('voice_post', { ip: curIp(), path: '/simulate', body: JSON.stringify({ text: t }) }); }
  catch (e) { addRow('发送失败: ' + e, 'error'); }
};

// 仅播报: debug TTS passthrough, no Hermes involved.
$('aSayBtn').onclick = async () => {
  if (!invoke) return;
  const t = $('aSay').value.trim();
  if (!t) return;
  $('aSay').value = '';
  try { await invoke('voice_post', { ip: curIp(), path: '/say', body: JSON.stringify({ text: t }) }); }
  catch (e) { addRow('播报失败: ' + e, 'error'); }
};
$('aSay').addEventListener('keydown', e => { if (e.key === 'Enter') $('aSendBtn').click(); });

// 切大脑: POST /brain {preset} → 202 + job; progress/result arrive via the feed
// 'job' events (handleBrainJob). A synchronous 409/400 (precheck reject: not idle,
// missing key_env, invalid preset) rejects here → revert the dropdown + show why.
$('aBrainSel') && ($('aBrainSel').onchange = async e => {
  const sel = e.target;
  const preset = sel.value;
  if (!invoke || !online || brainSwitching || preset === brainPreset) return;
  brainSwitching = true;
  brainJob = null;
  sel.disabled = true;
  setBrainStatus('⏳ 提交切换…', 'info');
  try {
    const r = JSON.parse(await invoke('voice_post', {
      ip: curIp(), path: '/brain', body: JSON.stringify({ preset }),
    }));
    if (r && r.error) throw new Error(r.error);
    if (r && r.job_id) brainJob = r.job_id;   // then feed 'job' events drive it
  } catch (err) {
    brainSwitching = false;
    setBrainStatus('✗ ' + err, 'bad');
    addRow('⚠ 切换被拒:' + err, 'error');
    setTimeout(() => setBrainStatus(''), 8000);
    refreshBrain();   // snaps the dropdown back to the current preset
  }
});

// Service control buttons + boot-autostart checkbox (mirrors vision.js).
for (const [id, action, label] of [
  ['asvcRestart', 'restart', '重启'],
  ['asvcStop', 'stop', '停止'],
  ['asvcStart', 'start', '启动'],
]) {
  const btn = $(id);
  if (!btn) continue;
  btn.onclick = async () => {
    if (!invoke) return;
    btn.disabled = true;
    addRow(`${label}语音服务…`, 'ask');
    try {
      await invoke('voice_service', { ip: curIp(), action });
      addRow(`服务${label}完成`, 'ask');
    } catch (e) {
      addRow(`服务${label}失败: ${e}`, 'error');
    } finally {
      btn.disabled = false;
    }
  };
}

async function refreshSvcAuto() {
  const cb = $('asvcAuto');
  if (!cb || !invoke) return;
  try {
    const out = await invoke('voice_service', { ip: curIp(), action: 'is-enabled' });
    cb.checked = out.split('\n').some(l => l.trim() === 'enabled');
    cb.disabled = false;
  } catch { cb.disabled = true; }
}
$('asvcAuto') && ($('asvcAuto').onchange = async e => {
  const cb = e.target;
  cb.disabled = true;
  try {
    await invoke('voice_service', { ip: curIp(), action: cb.checked ? 'enable' : 'disable' });
    addRow(`开机自启已${cb.checked ? '开启' : '关闭'}`, 'ask');
  } catch (err) {
    addRow(`开机自启设置失败: ${err}`, 'error');
    cb.checked = !cb.checked;
  } finally {
    cb.disabled = false;
  }
});

// 清空: local display only — the daemon's own feed/history is untouched, and
// lastSeq stays put so we don't re-fetch what was just cleared. curAnswer must
// be dropped too: it points at a row that is about to leave the DOM, and a
// streaming answer would otherwise append into a detached element forever.
$('voClear').onclick = () => {
  $('vofeed').innerHTML = '';
  curAnswer = null;
};
