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
import { $, S, invoke, getCfg, setCfg } from './state.js';

// /state is a cheap projection of health, so the VAD gauge can poll far faster
// than the 1 s health loop without adding real load. Below ~400 ms the gauge
// stops tracking VAD (which flips on 320 ms audio chunks) and starts to stutter.
const VAD_MS    = 300;
const HEALTH_MS = 1000;
const PROBE_MS  = 2500;
const FEED_MS   = 400;

let active = false, online = false;
let healthTimer = null, feedTimer = null, vadTimer = null;
let lastSeq = 0;
// Everything at or below this seq was cleared and must never be re-rendered.
// Persisted: the daemon keeps a 200-event ring and replays it from 0 on every
// reconnect, so a clear that only emptied the DOM came straight back on restart.
let clearedSeq = +getCfg('agentClearedSeq', 0) || 0;
let vstate = 'idle';        // daemon state machine mirror
let deadline = 0;           // 常开窗口 server-side deadline (epoch s), 0 = none
let curAnswer = null;       // the assistant bubble currently receiving deltas

// brain strip state
let brainSwitching = false; // a /brain job is in flight (select frozen)
let brainJob = null;        // current job_id we are tracking
let brainPreset = null;     // last-known selected preset (to revert on failure)
let presetMap = {};         // last-known presets, for labelling by preset key

function curIp() { return ($('voip') && $('voip').value.trim()) || '127.0.0.1'; }

// What to CALL a brain. The preset key ("local-9b", "deepseek") is a config
// handle, not a name a person picks a model by — two of them differing only in
// quantisation would read identically. So label by the model itself, and keep the
// key as the option's value: the switch API still speaks keys.
//
//   deepseek  -> deepseek-v4-flash
//   local-9b  -> Qwen3.5-9B-8bit(Local)
//   omni-mac  -> omni-mac(Local)
//
// The HF org prefix ("mlx-community/") is packaging and gets dropped; the quant
// suffix does NOT — it is the difference between two otherwise identical presets,
// and the whole point of running local models is comparing them.
//
// "(Local)" is decided by the endpoint being http://, which is sound rather than
// convenient: voice_brain._check_api only accepts a plaintext scheme for a
// private IP, so http:// here provably means the LAN.
//
// omni has no `model` in its preset — deliberately, since the daemon's drift
// check compares that field against config.yaml and omni does not go through the
// gateway at all. The board therefore does not know which model the Mac loaded,
// and falls back to the key rather than inventing a name.
function brainLabel(name, preset) {
  const p = preset || presetMap[name] || {};
  const endpoint = p.api || p.url || '';
  const model = p.model || '';
  const base = model ? model.split('/').pop() : name;
  return endpoint.startsWith('http://') ? base + '(Local)' : base;
}

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
    // The daemon stamps the preset KEY here. Show the model, same as the
    // dropdown: when you are A/B-ing two local models, "which one said this"
    // is the entire question, and the key does not answer it.
    badge.textContent = brainLabel(brain);   // untrusted; textContent only
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
    case 'user_text': {
      curAnswer = null;                       // next deltas start a new bubble
      // Show the VAD segment's length/level next to the transcript: a 0.4s blip
      // that decoded into a plausible sentence is almost certainly room noise,
      // and you cannot tell that from the words alone. Typed input has no segment.
      // addRow returns the .capmsg div, NOT the row — .capmeta is its SIBLING, so
      // querying it from here finds nothing and the numbers silently never appear.
      // (Shipped that way once; the 'tts' branch below got it right by accident,
      // which is why the answer rows had a duration and the 🗣 rows did not.)
      const msg = addRow('🗣 ' + ev.text, 'ask');
      const slot = msg && msg.parentElement.querySelector('.capmeta');
      if (slot && ev.secs !== undefined) {
        const s = document.createElement('span');
        s.textContent = ` ${ev.secs.toFixed(1)}s`
          + (ev.peak_dbfs !== undefined ? ` ${ev.peak_dbfs}dB` : '');
        slot.appendChild(s);
      }
      break;
    }
    case 'assistant_delta':
      if (!curAnswer) curAnswer = addRow('', 'answer', ev.brain);
      if (curAnswer) curAnswer.textContent += ev.delta || '';
      break;
    case 'tool':
      addRow('🔧 调用 ' + (ev.tool_name || '?') + ' …', 'ask');
      break;
    case 'tts':
      // Spoken length lands on the bubble it belongs to. It is only known once
      // the turn finished, so it is stamped onto the row after the fact rather
      // than shown as a floating "last utterance" number in the gauge.
      // `wait_s` is the half the operator actually feels — how long the robot sat
      // silent before the first sound. Spoken length alone cannot distinguish "the
      // brain was slow" from "the answer was long", and those need opposite fixes.
      if (ev.secs && curAnswer && curAnswer.parentElement) {
        const m = curAnswer.parentElement.querySelector('.capmeta');
        if (m && !m.dataset.secs) {
          m.dataset.secs = '1';
          const s2 = document.createElement('span');
          s2.textContent = (ev.wait_s ? ` 想${ev.wait_s.toFixed(1)}s` : '')
            + ` 说${ev.secs.toFixed(1)}s`;
          m.appendChild(s2);
        }
      }
      // omni speaks with the model's own voice — no TTS engine is involved, so
      // the "云端 TTS 降级" note would be plainly false there.
      if (ev.backend && ev.backend !== 'edge') {
        const st = $('aStage');
        if (st) st.textContent = ev.backend === 'omni'
          ? '模型原生语音' : '本地音色播报(云端 TTS 降级)';
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
    // Same label as the dropdown, or the two disagree about what just happened.
    const label = ev.preset ? brainLabel(ev.preset) : '';
    setBrainStatus('✓ 已切换到 ' + label, 'ok');
    addRow('🧠 大脑已切换到 ' + label, 'sys');
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

// "remote" is an engine id, not a place a reader can picture — the strip and the
// chain line both describe WHERE the turn goes, so spell it out there.
function asrDesc(asr) {
  return asr === 'remote' ? '电脑ASR(局域网)' : (asr || '—');
}

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
  presetMap = presets;        // so a job event can label by key alone
  const sel = $('aBrainSel');
  if (sel) {
    sel.innerHTML = '';
    for (const name of Object.keys(presets)) {
      const o = document.createElement('option');
      o.value = name;         // the switch API speaks preset keys, not labels
      o.textContent = brainLabel(name, presets[name]);   // untrusted; textContent only
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
  // A preset carries its own kind; absent means hermes (every preset that
  // predates the omni brain is one). omni has no TTS at all, so showing it a
  // "搭配 TTS x" line would describe an engine that never runs.
  const cur = presets[brain.preset];
  const kind = (cur && cur.kind) || 'hermes';
  const pairEl = $('aBrainPair');
  if (pairEl) {
    const pair = cur && cur.pair;
    const asr = asrDesc(pair && pair.asr);
    pairEl.textContent = kind === 'omni'
      ? `搭配 ASR ${asr}(仅转写) · 原生语音`
      : (pair ? `搭配 ASR ${asr} · TTS ${ttsDesc(pair.tts)}` : '搭配 —');
  }
  const chain = $('aChain');
  if (chain) {
    const pair = cur && cur.pair;
    const asr = asrDesc(pair && pair.asr);
    chain.textContent = kind === 'omni'
      ? `麦克风 → ${asr} 识别(仅显示文字) → omni 大脑(局域网，听原始音频) → 模型原生语音`
        + `（voice-daemon，端口 8092，Bearer 鉴权）。半双工：播报中闭麦，可随时打断。`
      : `麦克风 → ${asr} 识别 → Hermes(${(cur && cur.model) || brain.preset || '—'})`
        + ` → ${ttsDesc(pair && pair.tts)} 播报`
        + `（voice-daemon，端口 8092，Bearer 鉴权）。半双工：播报中闭麦，可随时打断。`;
  }
  // 开机默认对话 — a plain persisted flag, mirrored from `desired` so a config
  // hand-edit or another GUI shows up here. Default true matches the daemon's own
  // `config.get("auto_listen", True)`; disagreeing would paint a lie.
  const al = $('aAutoListen');
  if (al && !al.dataset.busy) {
    al.checked = desired.auto_listen !== false;
    al.disabled = !online;
  }
  const drift = $('aBrainDrift');
  if (drift) {
    const d = cfg && cfg.drift;
    const has = d && (Array.isArray(d) ? d.length : Object.keys(d).length);
    drift.style.display = has ? '' : 'none';
  }
}

$('aAutoListen') && ($('aAutoListen').onchange = async e => {
  const cb = e.target;
  const want = cb.checked;
  cb.dataset.busy = '1';       // keep the poll from repainting a stale value
  cb.disabled = true;
  try {
    await invoke('voice_post', { ip: curIp(), path: '/config',
                                 body: JSON.stringify({ axis: 'auto_listen', value: want }) });
    addRow(`开机默认对话已${want ? '开启' : '关闭'}(下次启动语音服务生效)`, 'sys');
  } catch (err) {
    cb.checked = !want;
    addRow('开机默认对话设置失败: ' + err, 'error');
  } finally {
    delete cb.dataset.busy;
    cb.disabled = false;
  }
});

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

// VAD gauge. `vad_since` is the epoch of the last phase flip, so "how long has it
// been like this" is computed here rather than counted client-side — a client-side
// counter drifts across daemon restarts and page reloads.
let vadSt = null;

function paintVad() {
  const bar = $('vadbar');
  if (!bar) return;
  if (!online || !vadSt) {
    bar.className = 'off';
    $('vadPhase').textContent = '离线';
    $('vadElapsed').textContent = '—';
    return;
  }
  // Phase order matters: once a segment closes, the turn owns the time, not VAD.
  // ASR is split out from the brain because "thinking" alone cannot tell you
  // which of the two a slow turn is actually stuck in.
  let phase, cls;
  if (vstate === 'thinking')      { phase = vadSt.asr_busy ? 'ASR 识别中' : '大脑思考中';
                                    cls = 'busy'; }
  else if (vstate === 'speaking') { phase = '播报中';      cls = 'busy'; }
  else if (vstate === 'idle')     { phase = '待机(闭麦)';  cls = 'off'; }
  else if (vadSt.vad_active)      { phase = '采集中';      cls = 'cap'; }
  else                            { phase = '空闲';        cls = 'off'; }
  bar.className = cls;
  $('vadPhase').textContent = phase;

  // Elapsed is only meaningful for the VAD phases; a turn's clock is its own.
  const since = +vadSt.vad_since || 0;
  const held = since ? (Date.now() / 1000 - since) : 0;
  $('vadElapsed').textContent = (cls === 'busy' || !since)
    ? '—' : held.toFixed(1) + 's';

  const dbfs = vadSt.mic_dbfs;
  $('vadLevel').textContent = (dbfs === undefined || dbfs === null)
    ? '—' : dbfs.toFixed(0) + 'dB';
  // Segment and reply lengths are stamped on the transcript rows they belong to,
  // not shown here: a floating "most recent" number leaves you matching it back
  // to a line by hand. The gauge keeps only what has no row of its own.
  //
  // Ignored turns are the noise counter: if this climbs while nobody is
  // talking to the robot, VAD/echo is firing turns that should never have started.
  const ig = $('vadIgnored');
  if (ig) ig.textContent = vadSt.ignored ? String(vadSt.ignored) : '0';
}

async function pollVad() {
  if (!active || !online || !invoke) { paintVad(); return; }
  try {
    vadSt = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/state' }));
    vstate = vadSt.state || vstate;      // fresher than the 1 s health loop
  } catch { /* transient; health loop owns online/offline */ }
  paintVad();
}

// Camera thumbnails. Auxiliary to the conversation, so they refresh far slower
// than the Vision tab's viewfinder — the question here is only "what is it
// looking at", not "is the framing right".
const CAM_MS = 1200;
let camPumping = false;

async function camPump() {
  if (camPumping) return;
  camPumping = true;
  try {
    while (active && online && invoke) {
      for (const [cam, imgId, ageId] of [['front', 'acamFront', 'acamFrontAge'],
                                         ['wrist', 'acamWrist', 'acamWristAge']]) {
        if (!active || !online) break;
        const img = $(imgId), age = $(ageId);
        try {
          const r = JSON.parse(await invoke('vlm_frame',
                                            { ip: curIp(), camera: cam }));
          if (img && r.b64) { img.src = 'data:image/jpeg;base64,' + r.b64;
                              img.classList.add('live'); }
          if (age) { age.textContent = 'live'; age.classList.remove('stale'); }
        } catch {
          // One camera failing must not kill the pump or blank the other one;
          // mark it stale and keep the last good frame on screen.
          if (age) { age.textContent = '离线'; age.classList.add('stale'); }
          if (img) img.classList.remove('live');
        }
      }
      await new Promise(r => setTimeout(r, CAM_MS));
    }
  } finally {
    camPumping = false;
  }
}

async function pollFeed() {
  if (!active || !online || !invoke) return;
  try {
    const r = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/feed?since=' + lastSeq }));
    // A watermark can never legally exceed the ring's newest seq. When one does,
    // it was written by a daemon process that no longer exists — seq restarts at 0
    // on restart — and every seq it names now belongs to unrelated events.
    //
    // Testing only `lastSeq` here was not enough, and the gap was invisible:
    // `lastSeq` is per-session and starts at 0, so after an app restart it is
    // BELOW the fresh ring and the guard stays quiet, while `clearedSeq` is
    // persisted and sails straight past. The whole feed then vanishes with the
    // daemon working perfectly — talking out loud with an empty transcript.
    if (r.last_seq < Math.max(lastSeq, clearedSeq)) {
      lastSeq = 0;
      if (clearedSeq) { clearedSeq = 0; setCfg('agentClearedSeq', 0); }
      return;
    }
    for (const ev of r.events || []) {
      if (ev.seq !== undefined && ev.seq <= clearedSeq) continue;   // cleared
      handleEvent(ev);
    }
    lastSeq = r.last_seq;
  } catch { /* transient; health loop owns online/offline */ }
}

function goOnline() {
  online = true;
  lastSeq = 0;               // full replay of the ring on (re)connect
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, HEALTH_MS);
  if (!feedTimer) feedTimer = setInterval(pollFeed, FEED_MS);
  if (!vadTimer) vadTimer = setInterval(pollVad, VAD_MS);
  camPump();                 // self-terminating loop; guarded against double-start
  refreshSvcAuto();
  refreshBrain();
  paintState();
}

function goOffline() {
  online = false;
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, PROBE_MS);
  if (feedTimer) { clearInterval(feedTimer); feedTimer = null; }
  if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
  vadSt = null; paintVad();
  const al = $('aAutoListen'); if (al) al.disabled = true;
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
  for (const t of [healthTimer, feedTimer, vadTimer]) if (t) clearInterval(t);
  healthTimer = feedTimer = vadTimer = null;
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

// Service control + autostart now live on the Voice page (they are operations,
// not conversation), so their handlers moved to voicelab.js — a button whose
// feedback lands in a feed on another page is worse than no feedback.
// refreshSvcAuto stays a no-op hook here so goOnline() keeps its shape.
function refreshSvcAuto() {}

// 清空: local display only — the daemon's own feed/history is untouched, and
// lastSeq stays put so we don't re-fetch what was just cleared. curAnswer must
// be dropped too: it points at a row that is about to leave the DOM, and a
// streaming answer would otherwise append into a detached element forever.
// 新对话: this is a /new, not a cosmetic wipe. Clearing only the local
// transcript leaves the robot still remembering everything you just said, so the
// "fresh start" would be a fresh start for the human alone. Single-user by
// design, so there is no other session to protect.
$('voClear').onclick = async () => {
  $('vofeed').innerHTML = '';
  curAnswer = null;
  // Remember where we cleared, or the next reconnect replays it all back.
  clearedSeq = lastSeq;
  setCfg('agentClearedSeq', clearedSeq);
  if (!invoke) return;
  try {
    const r = JSON.parse(await invoke('voice_post',
      { ip: curIp(), path: '/reset', body: '{}' }));
    addRow(r.reset ? `🆕 新对话（大脑已忘记之前 ${r.dropped_exchanges ?? 0} 轮）`
                   : `🆕 本机记录已清空，但大脑未重置：${r.detail || '未知原因'}`,
           'sys');
  } catch (e) {
    addRow('🆕 本机记录已清空，但大脑重置失败: ' + e, 'error');
  }
};
