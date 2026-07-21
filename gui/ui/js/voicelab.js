// Voice tab (#page-voice, data-page="voice"): device / ASR / TTS debug console.
//
// Same trust boundary as agent.js / vision.js: the Rust backend owns the bearer
// token; we only poll Tauri commands (voice_get/voice_post) and paint. This page
// talks to the SAME voice-daemon as the Agent page and deliberately reuses its
// #voip field for the board IP — one daemon, one connection field (no second
// source of truth).
//
// Four blocks (see docs/agent-voice-pages-plan.html §4.2):
//   1. Device status bar  — mic card + live dBFS + 3s peak, speaker card,
//      audio_ok light. Data from GET /health (capture_card/playback_card/audio/
//      mic_dbfs/mic_peak_dbfs — verified against daemon.py, code is authority).
//   2. ASR transcription  — POST /asr_debug {on:1/0}; GET /asr_debug/tail?since=
//      polled at 400ms while the page is active AND debug is on.
//   3. TTS audition       — engine/voice dropdowns (GET /config enums), POST /say
//      to audition, POST /config {tts, ephemeral:true} on change (no persist),
//      POST /config {tts} (no ephemeral) to persist into the current pair.
//   4. Vision speak switch — POST /config {vision_speak}. Bridge lives on the
//      board; the checkbox is just the switch.
//
// The /config, /asr_debug and /asr_debug/tail endpoints are added by the daemon
// P0b work; every call is defensive so a not-yet-deployed daemon degrades
// gracefully instead of throwing.
import { $, S, invoke } from './state.js';

const HEALTH_MS = 1000;   // 1 Hz device telemetry while online
const DBG_HEALTH_MS = 300; // faster telemetry in DEBUG — 1Hz misses 320ms speech peaks
const PROBE_MS  = 2500;   // slow reconnect probe while offline
const TAIL_MS   = 400;    // ASR transcript tail poll, only while transcribing

// dBFS thresholds (from .memory/voice-frontend-s2.md, MCP01 field rules):
//   >= -34  → energy gate open, real speech level (green)
//   ~ -79   → device muted / not powered (long-press power 3s)
const LVL_MIN  = -80;     // meter floor
const LVL_GATE = -34;     // BARGE_MIN_RMS=0.02 — "level is enough"
const LVL_MUTE = -70;     // at/below this the mic is effectively silent

let active = false, online = false;
let healthTimer = null, tailTimer = null;
let asrOn = false;        // ASR debug transcription is running
let tailSeq = 0;          // /asr_debug/tail cursor
let partialRow = null;    // the single live partial-transcript row (overwritten)
let vadEnums = [];        // enums.vad from GET /config (engine availability + defaults)
let vadFlashUntil = 0;    // wall-clock ms until the "just cut a segment" yellow dot ends

// Health cadence: fast (300ms) in DEBUG so the level meter tracks speech peaks; 1Hz
// otherwise; slow probe while offline. Re-armed whenever online/asrOn change.
function healthEvery() { return online ? (asrOn ? DBG_HEALTH_MS : HEALTH_MS) : PROBE_MS; }
function armHealth() {
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(pollHealth, healthEvery());
}

function curIp() { return ($('voip') && $('voip').value.trim()) || '127.0.0.1'; }

// ---- feed rendering ------------------------------------------------------

function pad2(n) { return String(n).padStart(2, '0'); }

function addRow(feedId, text, kind) {
  const feed = $(feedId);
  if (!feed) return null;
  const t = new Date();
  const row = document.createElement('div');
  row.className = 'caprow' + (kind ? ' cap-' + kind : '');
  const textwrap = document.createElement('div');
  textwrap.className = 'captext';
  const meta = document.createElement('div');
  meta.className = 'capmeta';
  meta.textContent = `${pad2(t.getHours())}:${pad2(t.getMinutes())}:${pad2(t.getSeconds())}`;
  const msg = document.createElement('div');
  msg.className = 'capmsg';
  msg.textContent = text;   // untrusted ASR / model output
  textwrap.append(meta, msg);
  row.append(textwrap);
  feed.insertBefore(row, feed.firstChild);   // newest on top
  while (feed.childElementCount > 200) feed.removeChild(feed.lastChild);
  return msg;
}

// ---- device status bar ---------------------------------------------------

// VAD dot: ⚪ silent (grey) / 🟢 listening (vad_active) / 🟡 just cut a segment (1s).
function paintVadDot(h) {
  const dot = $('vdVadDot'), lab = $('vdVadState');
  if (!dot) return;
  if (!online || !h) { dot.textContent = '⚪'; dot.className = 'vaddot'; if (lab) lab.textContent = '—'; return; }
  if (Date.now() < vadFlashUntil) {
    dot.textContent = '🟡'; dot.className = 'vaddot flash'; if (lab) lab.textContent = '刚截断';
  } else if (h.vad_active) {
    dot.textContent = '🟢'; dot.className = 'vaddot live'; if (lab) lab.textContent = '在听';
  } else {
    dot.textContent = '⚪'; dot.className = 'vaddot'; if (lab) lab.textContent = '静音';
  }
}

function paintDevice(h) {
  const okPill = $('vdAudioOk');
  const fill = $('vdMicFill');
  const hint = $('vdHint');
  paintVadDot(h);
  if (!online || !h) {
    if (okPill) { okPill.textContent = '离线'; okPill.className = 'pill bad'; }
    $('vdMicCard').textContent = '麦克风 —';
    $('vdPlayCard').textContent = '音响 —';
    $('vdMicDbfs').textContent = '—';
    $('vdMicPeak').textContent = '峰 —';
    if (fill) { fill.style.width = '0'; fill.classList.remove('hot'); }
    if (hint) hint.textContent = '';
    return;
  }
  const cap = h.capture_card, play = h.playback_card;
  const dbfs = +h.mic_dbfs, peak = +h.mic_peak_dbfs;
  $('vdMicCard').textContent = '麦克风 ' + (cap || '未发现');
  $('vdPlayCard').textContent = '音响 ' + (play || '未发现');
  $('vdMicDbfs').textContent = Number.isFinite(dbfs) ? dbfs.toFixed(0) + ' dBFS' : '—';
  $('vdMicPeak').textContent = '峰 ' + (Number.isFinite(peak) ? peak.toFixed(0) : '—');
  if (fill) {
    const pct = Math.max(0, Math.min(100, (dbfs - LVL_MIN) / (0 - LVL_MIN) * 100));
    fill.style.width = pct + '%';
    fill.classList.toggle('hot', Number.isFinite(dbfs) && dbfs >= LVL_GATE);
  }
  if (okPill) {
    const ok = h.audio === 'ok';
    okPill.textContent = ok ? '音频就绪' : '音频缺失';
    okPill.className = 'pill ' + (ok ? 'ok' : 'bad');
  }
  // Built-in diagnosis (the MCP01 field rules, made visible instead of guessed):
  if (hint) {
    if (!cap || !play) {
      hint.textContent = '⚠ 声卡未发现:检查 USB 拔插,或在 Agent 页重启语音服务重发现';
    } else if (Number.isFinite(peak) && peak <= LVL_MUTE) {
      hint.textContent = '⚠ 电平≈静音:MCP01 未开机(长按电源键 3 秒)或静音键红灯亮';
    } else {
      hint.textContent = '';
    }
  }
}

// ---- TTS audition config (engine / voice dropdowns) ----------------------

// Engine enum entry → option label. Offline models carry a size tag:
// params (x.xB) when known, else measured disk footprint (xxxMB).
function engineLabel(e) {
  if (typeof e === 'string') return e;
  let lab = e.label || e.id;
  if (e.params_b != null) lab += ` (${e.params_b}B)`;
  else if (e.disk_mb != null) lab += ` (${e.disk_mb}MB)`;
  return lab;
}

function fillEngineSel(sel, list, keep) {
  if (!sel || !Array.isArray(list) || !list.length) return;
  const cur = keep || sel.value;
  sel.innerHTML = '';
  for (const e of list) {
    const id = typeof e === 'string' ? e : e.id;
    if (!id) continue;
    const o = document.createElement('option');
    o.value = id;
    o.textContent = engineLabel(e);   // untrusted config; textContent only
    sel.append(o);
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
}

// VAD engine dropdown: enums.vad carries {id,label,disk_mb,default_threshold,available}.
// Unavailable engines (sherpa without ten_vad, webrtcvad not installed) are greyed out
// and cannot be selected — the daemon also refuses them, no silent fallback.
function fillVadSel(list, curEngine) {
  const sel = $('vlVadEngine');
  if (!sel || !Array.isArray(list) || !list.length) return;
  vadEnums = list;
  sel.innerHTML = '';
  for (const e of list) {
    const o = document.createElement('option');
    o.value = e.id;
    let lab = e.label || e.id;
    if (e.disk_mb) lab += ` (${e.disk_mb}MB)`;
    if (e.available === false) { lab += ' · 不可用'; o.disabled = true; }
    o.textContent = lab;                 // untrusted config; textContent only
    sel.append(o);
  }
  if (curEngine && [...sel.options].some(o => o.value === curEngine)) sel.value = curEngine;
}

// Repaint an input from config — but never stomp the user: skip while it has
// focus (mid-typing), and skip whenever an ephemeral override is live (the GUI's
// own edits are then the truth; the persistent value would snap them back on
// every offline→online flap of the 300ms debug poll).
function paintInput(id, v, ephemeralLive) {
  const el = $(id);
  if (!el || v == null) return;
  if (ephemeralLive || document.activeElement === el) return;
  el.value = v;
}

function applyVadFromConfig(cfg) {
  const desired = (cfg && cfg.desired) || cfg || {};
  const en = desired.enums || cfg.enums || {};
  const vad = desired.vad || {};
  const eph = !!(cfg && cfg.drift && cfg.drift.ephemeral);
  fillVadSel(en.vad, vad.engine);
  paintInput('vlVadThreshold', vad.threshold, eph);
  paintInput('vlVadMinSpeech', vad.min_speech_s, eph);
  paintInput('vlVadMinSilence', vad.min_silence_s, eph);
  paintInput('vlVadPreRoll', vad.pre_roll_s, eph);
  const audio = desired.audio || {};
  paintInput('vlAudioGain', audio.gain_db, eph);
}

// The VAD params are text inputs (type=number silently eats keystrokes it deems
// invalid — full-width 「。」 from a Chinese IME included). Parse ourselves and
// normalize full-width decimal marks; the daemon's normalize_vad clamps ranges.
function numVal(id) {
  const raw = ($(id).value || '').replace(/[。，]/g, '.').trim();
  return parseFloat(raw);
}

function curVad() {
  return {
    engine: $('vlVadEngine').value,
    threshold: numVal('vlVadThreshold'),
    min_speech_s: numVal('vlVadMinSpeech'),
    min_silence_s: numVal('vlVadMinSilence'),
    pre_roll_s: numVal('vlVadPreRoll'),
  };
}

function curGain() {
  const g = parseFloat($('vlAudioGain').value);
  return Number.isFinite(g) ? g : 0;
}

function applyTtsFromConfig(cfg) {
  const desired = (cfg && cfg.desired) || cfg || {};
  const presets = desired.presets || {};
  const brain = desired.brain || {};
  const cur = presets[brain.preset];
  const pair = cur && cur.pair;
  const tts = pair && pair.tts;
  // Engine dropdowns from the daemon's enums (labels carry offline model sizes).
  const en = desired.enums || cfg.enums || {};
  fillEngineSel($('vlAsrEngine'), en.asr, pair && pair.asr);
  fillEngineSel($('vlTtsEngine'), en.tts);
  // Current engine / voice from the active pair.
  const engineSel = $('vlTtsEngine');
  if (engineSel && tts) {
    const eng = typeof tts === 'string' ? tts : tts.engine;
    if (eng) engineSel.value = eng;
  }
  // edge voice enumeration — accept a few plausible shapes from GET /config.
  const enums = desired.enums || cfg.enums || {};
  const voices = enums.edge_voices || enums.voices || [];
  const voiceSel = $('vlTtsVoice');
  if (voiceSel) {
    voiceSel.innerHTML = '';
    for (const v of voices) {
      const id = typeof v === 'string' ? v : (v.id || v.voice || v.name);
      const label = typeof v === 'string' ? v : (v.label || id);
      if (!id) continue;
      const o = document.createElement('option');
      o.value = id; o.textContent = label;
      voiceSel.append(o);
    }
    const curVoice = tts && typeof tts === 'object' ? tts.voice : null;
    if (curVoice) {
      if (![...voiceSel.options].some(o => o.value === curVoice)) {
        const o = document.createElement('option');
        o.value = curVoice; o.textContent = curVoice;
        voiceSel.append(o);
      }
      voiceSel.value = curVoice;
    }
  }
  // Vision speak switch + spoken-length cap reflect desired state.
  const vs = $('vlVisionSpeak');
  if (vs) vs.checked = !!desired.vision_speak;
  const vl = $('vlVisionLimit');
  if (vl && desired.vision_speak_limit != null) vl.value = desired.vision_speak_limit;
  applyVadFromConfig(cfg);
  syncTtsUi();
}

// edge exposes a voice picker; melo does not.
function syncTtsUi() {
  const wrap = $('vlTtsVoiceWrap');
  if (wrap) wrap.style.display = ($('vlTtsEngine').value === 'edge') ? '' : 'none';
}

function curTts() {
  const engine = $('vlTtsEngine').value;
  const tts = { engine };
  if (engine === 'edge') {
    const v = $('vlTtsVoice').value;
    if (v) tts.voice = v;
  }
  return tts;
}

async function refreshConfig() {
  if (!invoke) return;
  try {
    applyTtsFromConfig(JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/config' })));
  } catch { /* /config not up yet — dropdowns keep their static defaults */ }
}

async function postConfig(patch, feedNote) {
  if (!invoke) return;
  try {
    await invoke('voice_post', { ip: curIp(), path: '/config', body: JSON.stringify(patch) });
    if (feedNote) addRow('vlTtsFeed', feedNote, 'ask');
  } catch (e) {
    addRow('vlTtsFeed', '配置失败: ' + e, 'error');
  }
}

// ---- ASR transcription tail ----------------------------------------------

function addAsrEvent(ev) {
  const text = ev.text || '';
  if (ev.partial) {
    if (!partialRow) partialRow = addRow('vlAsrFeed', text, 'partial');
    else partialRow.textContent = text;
  } else {
    // A final commits: drop the live partial row it was refining, add a clean one.
    if (partialRow) { const r = partialRow.closest('.caprow'); if (r) r.remove(); partialRow = null; }
    if (text) addRow('vlAsrFeed', text, '');
  }
}

// Segment rows carry the outcome (why a VAD segment did/didn't become text) plus
// a "▶ 听" button that replays the exact PCM the model heard.
const OUTCOME_LABEL = {
  accepted: '✓ 出字', empty_asr: '解码空', filler: '语气词',
  too_short: '过短', gate: '能量门',
};

function addSegRow(ev) {
  const feed = $('vlAsrFeed');
  if (!feed) return;
  vadFlashUntil = Date.now() + 1000;     // a segment just landed → flash the dot yellow
  const t = new Date();
  const row = document.createElement('div');
  row.className = 'caprow cap-seg seg-' + (ev.outcome || '');
  const textwrap = document.createElement('div');
  textwrap.className = 'captext';
  const meta = document.createElement('div');
  meta.className = 'capmeta';
  const lvl = [ev.dur_s != null ? ev.dur_s + 's' : null,
               ev.peak_dbfs != null ? '峰' + ev.peak_dbfs + 'dB' : null]
    .filter(Boolean).join(' ');
  meta.textContent = `${pad2(t.getHours())}:${pad2(t.getMinutes())}:${pad2(t.getSeconds())}`
    + (lvl ? ' · ' + lvl : '');
  const msg = document.createElement('div');
  msg.className = 'capmsg';
  msg.textContent = (ev.text && ev.text.trim()) ? ev.text : '(空)';   // untrusted
  const badge = document.createElement('span');
  badge.className = 'segbadge';
  badge.textContent = OUTCOME_LABEL[ev.outcome] || ev.outcome || '';
  textwrap.append(meta, msg, badge);
  if (ev.seg_id) {
    // 板上播(默认推荐):机器人音响放,daemon 播放期自动闭麦防回录
    const board = document.createElement('button');
    board.className = 'minibtn';
    board.textContent = '▶ 听';
    board.title = '在机器人音响播,自动闭麦防回录';
    board.onclick = () => playSegBoard(ev.seg_id, board);
    // 本机播(远程调试用):Web Audio 在这台机器出声
    const local = document.createElement('button');
    local.className = 'minibtn';
    local.textContent = '🎧';
    local.title = '本机播,远程调试用';
    local.onclick = () => playSeg(ev.seg_id, local);
    textwrap.append(board, local);
  }
  row.append(textwrap);
  feed.insertBefore(row, feed.firstChild);
  while (feed.childElementCount > 200) feed.removeChild(feed.lastChild);
}

// WKWebView rejects HTMLAudio.play() once the click gesture is "spent" by an
// async hop (and an un-awaited play() fails silently). Web Audio instead: the
// context is created/resumed synchronously inside the click, then decoded
// buffers may start at any later time.
let audioCtx = null;

async function playSeg(id, btn) {
  if (!invoke) return;
  btn.disabled = true;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();   // still in the gesture
    const r = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/asr_debug/seg?id=' + id }));
    if (!r.wav_b64) { addRow('vlAsrFeed', '段音频已被覆盖', 'error'); return; }
    const bin = atob(r.wav_b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const buf = await audioCtx.decodeAudioData(bytes.buffer);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start();
  } catch (e) {
    addRow('vlAsrFeed', '回放失败: ' + e, 'error');
  } finally {
    btn.disabled = false;
  }
}

// 板上播:daemon 用 aplay 在机器人音响出声,播放期自动闭麦防回录。默认推荐。
async function playSegBoard(id, btn) {
  if (!invoke) return;
  btn.disabled = true;
  try {
    const r = JSON.parse(await invoke('voice_post',
      { ip: curIp(), path: '/asr_debug/seg_play', body: JSON.stringify({ id }) }));
    if (r.error) addRow('vlAsrFeed', '板上回放失败: ' + r.error, 'error');
  } catch (e) {
    addRow('vlAsrFeed', '板上回放失败: ' + e, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function pollTail() {
  if (!active || !online || !asrOn || !invoke) return;
  try {
    const r = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/asr_debug/tail?since=' + tailSeq }));
    if (r.last_seq != null && r.last_seq < tailSeq) { tailSeq = 0; return; }  // daemon restart
    for (const ev of r.events || []) {
      // seg rows (kind 'seg' / outcome-bearing) render with outcome + replay;
      // legacy partial/final rows fall through to addAsrEvent.
      if (ev.kind === 'seg' || ev.outcome || ev.seg_id != null) addSegRow(ev);
      else addAsrEvent(ev);
    }
    if (r.last_seq != null) tailSeq = r.last_seq;
  } catch { /* transient; health loop owns online/offline */ }
}

function startTail() {
  if (!tailTimer) tailTimer = setInterval(pollTail, TAIL_MS);
  pollTail();
}
function stopTail() {
  if (tailTimer) { clearInterval(tailTimer); tailTimer = null; }
}

async function setAsr(on) {
  if (!invoke || !online) return;
  try {
    await invoke('voice_post', { ip: curIp(), path: '/asr_debug', body: JSON.stringify({ on: on ? 1 : 0 }) });
    asrOn = on;
    paintAsrBtn();
    armHealth();               // DEBUG → 300ms telemetry so the level meter tracks peaks
    if (on) { tailSeq = 0; startTail(); }
    else { stopTail(); partialRow = null; }
  } catch (e) {
    addRow('vlAsrFeed', '转写开关失败: ' + e, 'error');
  }
}

function paintAsrBtn() {
  const btn = $('vlAsrBtn');
  if (!btn) return;
  btn.disabled = !online;
  btn.textContent = asrOn ? '停止转写' : '开始转写';
  btn.classList.toggle('live', asrOn);
}

// ---- polling / online-offline (mirrors agent.js cadence) -----------------

async function pollHealth() {
  if (!active || !invoke) return;
  try {
    const h = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/health' }));
    if (!online) goOnline();
    paintDevice(h);
    // The daemon owns the DEBUG state (another window / a restart may flip it):
    // mirror it instead of trusting our local toggle.
    const debug = h.state === 'debug';
    if (debug !== asrOn) {
      asrOn = debug;
      paintAsrBtn();
      armHealth();
      if (asrOn) startTail(); else { stopTail(); partialRow = null; }
    }
  } catch {
    if (online) goOffline();
    else paintDevice(null);
  }
}

function goOnline() {
  online = true;
  armHealth();
  paintAsrBtn();
  const tb = $('vlTtsBtn'); if (tb) tb.disabled = false;
  const vs = $('vlVisionSpeak'); if (vs) vs.disabled = false;
  const st = $('vdSelftest'); if (st) st.disabled = false;
  refreshConfig();
}

function goOffline() {
  online = false;
  asrOn = false;
  stopTail();
  partialRow = null;
  armHealth();
  paintAsrBtn();
  const tb = $('vlTtsBtn'); if (tb) tb.disabled = true;
  const vs = $('vlVisionSpeak'); if (vs) vs.disabled = true;
  const st = $('vdSelftest'); if (st) st.disabled = true;
  paintDevice(null);
}

// ---- activation ----------------------------------------------------------
// Polling only: leaving the tab stops /health + tail polling. It does NOT stop
// the daemon's ASR-debug state — that is a board-side toggle the operator owns
// (same "the session lives on the daemon" philosophy as the Agent page).

function startActive() {
  if (active || S.page !== 'voice') return;
  active = true;
  online = false;
  healthTimer = setInterval(pollHealth, PROBE_MS);
  pollHealth();
}

function stopActive() {
  if (!active) return;
  active = false;
  stopTail();
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = null;
  online = false;
  paintDevice(null);
}

export function onEnterVoice() { startActive(); }
export function onLeaveVoice() { stopActive(); }

// ---- wiring --------------------------------------------------------------

$('vlAsrBtn') && ($('vlAsrBtn').onclick = () => { if (online) setAsr(!asrOn); });
$('vlAsrClear') && ($('vlAsrClear').onclick = () => { $('vlAsrFeed').innerHTML = ''; partialRow = null; });

// TTS engine / voice change → ephemeral override (does NOT persist).
// POST /config body shape is {axis, value, ephemeral} — by-axis whole replacement.
$('vlTtsEngine') && ($('vlTtsEngine').onchange = () => {
  syncTtsUi();
  postConfig({ axis: 'tts', value: curTts(), ephemeral: true },
             '临时切引擎: ' + $('vlTtsEngine').value);
});
$('vlTtsVoice') && ($('vlTtsVoice').onchange = () => {
  postConfig({ axis: 'tts', value: curTts(), ephemeral: true },
             '临时切音色: ' + $('vlTtsVoice').value);
});

// 保存为当前搭配: persist current engine/voice into the pair (no ephemeral).
$('vlTtsSave') && ($('vlTtsSave').onclick = () => {
  postConfig({ axis: 'tts', value: curTts() }, '已保存为当前搭配');
});

// ---- VAD engine / params + digital gain (global audio front-end) ----------
// All changes are ephemeral (debug override, auto-reverts on leaving DEBUG); the
// small 「存」 button is the only thing that persists. VAD is NOT part of a preset
// pair — it is the global front-end, so it gets its own save, not the pair's.
$('vlVadEngine') && ($('vlVadEngine').onchange = () => {
  const e = vadEnums.find(x => x.id === $('vlVadEngine').value);
  if (e && e.default_threshold != null) $('vlVadThreshold').value = e.default_threshold;
  postConfig({ axis: 'vad', value: curVad(), ephemeral: true },
             '临时切 VAD: ' + $('vlVadEngine').value);
});
for (const id of ['vlVadThreshold', 'vlVadMinSpeech', 'vlVadMinSilence', 'vlVadPreRoll']) {
  $(id) && ($(id).onchange = () => {
    postConfig({ axis: 'vad', value: curVad(), ephemeral: true }, '临时改 VAD 参数');
  });
}
$('vlAudioGain') && ($('vlAudioGain').onchange = () => {
  postConfig({ axis: 'audio', value: { gain_db: curGain() }, ephemeral: true },
             '临时增益: ' + curGain() + ' dB');
});
// 存: persist VAD engine/params + gain into config (non-ephemeral).
$('vlVadSave') && ($('vlVadSave').onclick = () => {
  postConfig({ axis: 'vad', value: curVad() }, '已保存 VAD 前端');
  postConfig({ axis: 'audio', value: { gain_db: curGain() } });
});

// 播报: audition through POST /say; echo backend + first-byte if the daemon
// returns them in the response body (the /feed tts event also carries them,
// but this page intentionally does not poll /feed).
async function audition() {
  if (!invoke || !online) return;
  const t = $('vlTtsText').value.trim();
  if (!t) return;
  const btn = $('vlTtsBtn');
  btn.disabled = true;
  try {
    const raw = await invoke('voice_post', { ip: curIp(), path: '/say', body: JSON.stringify({ text: t }) });
    let note = '▶ ' + t;
    try {
      const r = JSON.parse(raw);
      const tail = [r.backend, r.first_byte_ms != null ? r.first_byte_ms + 'ms' : null]
        .filter(Boolean).join(' · ');
      if (tail) note += '  [' + tail + ']';
    } catch { /* /say may return no JSON body — just echo the text */ }
    addRow('vlTtsFeed', note, 'answer');
  } catch (e) {
    addRow('vlTtsFeed', '播报失败: ' + e, 'error');
  } finally {
    btn.disabled = !online;
  }
}
$('vlTtsBtn') && ($('vlTtsBtn').onclick = audition);
$('vlTtsText') && $('vlTtsText').addEventListener('keydown', e => { if (e.key === 'Enter') audition(); });

// 回环自检: feed a known human-voice clip straight through VAD+ASR (no mic).
// Bisects "acoustic problem" vs "model problem" — passes even with MCP01 absent.
async function runSelftest() {
  if (!invoke || !online) return;
  const btn = $('vdSelftest');
  btn.disabled = true;
  addRow('vlAsrFeed', '回环自检运行中…', 'ask');
  try {
    const r = JSON.parse(await invoke('voice_post', { ip: curIp(), path: '/selftest', body: '{}' }));
    if (r.error) {
      addRow('vlAsrFeed', '自检错误: ' + r.error, 'error');
    } else {
      const note = `回环自检 ${r.pass ? '通过 ✓' : '未通过 ✗'} · 识别「${r.asr_text || '(空)'}」`
        + ` · 期望「${r.expected}」· 段${r.vad_segments} · 相似度${r.ratio}`;
      addRow('vlAsrFeed', note, r.pass ? 'answer' : 'error');
    }
  } catch (e) {
    addRow('vlAsrFeed', '自检失败: ' + e, 'error');
  } finally {
    btn.disabled = !online;
  }
}
$('vdSelftest') && ($('vdSelftest').onclick = runSelftest);

// Vision spoken-length cap → POST /config {vision_speak_limit} (immediate persist).
$('vlVisionLimit') && ($('vlVisionLimit').onchange = () => {
  let n = parseInt($('vlVisionLimit').value, 10);
  if (!Number.isFinite(n)) { n = 300; }
  n = Math.max(20, Math.min(2000, n));
  $('vlVisionLimit').value = n;
  postConfig({ axis: 'vision_speak_limit', value: n }, '播报长度上限: ' + n);
});

// Vision speak switch → board-side bridge toggle.
$('vlVisionSpeak') && ($('vlVisionSpeak').onchange = async e => {
  const cb = e.target;
  cb.disabled = true;
  try {
    await invoke('voice_post', { ip: curIp(), path: '/config',
      body: JSON.stringify({ axis: 'vision_speak', value: cb.checked }) });
  } catch {
    cb.checked = !cb.checked;   // revert on failure
  } finally {
    cb.disabled = !online;
  }
});
