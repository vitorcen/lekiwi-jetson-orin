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
  // 空框没有用户编辑要保护 —— 永远 seed(否则一个 asr/tts 的 ephemeral 覆盖会让整组
  // VAD 参数框刷新后空着,即"参数值不见了"的 bug)。只有已有值且被编辑/覆盖时才跳过。
  if (el.value !== '' && (ephemeralLive || document.activeElement === el)) return;
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

// ---- 两级:一级识别模式(vlRecMode) + 二级模型(vlModelSel,随模式变) ----
let asrEnums = [], streamEnums = [], curAsr = '', curStreamModel = 'zh-2025';

function recMode() { return $('vlRecMode') ? $('vlRecMode').value : 'vad'; }

// 二级模型下拉:VAD→离线引擎枚举,流式→流式模型枚举。eph 时保留用户选择不 stomp。
function fillModelSel(eph) {
  const sel = $('vlModelSel');
  if (!sel) return;
  const stream = recMode() === 'stream';
  const list = stream ? streamEnums : asrEnums;
  const want = stream ? curStreamModel : curAsr;
  const keep = (eph && sel.value) ? sel.value : want;   // ephemeral 覆盖时不回抢
  fillEngineSel(sel, list, keep);
}

// 参数区随模式显隐(VAD 参数 vs 端点静音)
function applyModeUI() {
  const stream = recMode() === 'stream';
  if ($('vlVadRow')) $('vlVadRow').style.display = stream ? 'none' : '';
  if ($('vlStreamRow')) $('vlStreamRow').style.display = stream ? '' : 'none';
}

function curStream() {
  const stream = recMode() === 'stream';
  return { enabled: stream,
           model: stream ? ($('vlModelSel').value || curStreamModel) : curStreamModel,
           endpoint_silence_s: numVal('vlStreamSilence') };
}

// 从 config 回填一级模式 + 二级模型 + 参数(与 VAD 同语义:ephemeral 时不覆盖用户改动)。
function applyStreamFromConfig(cfg) {
  const desired = (cfg && cfg.desired) || cfg || {};
  const st = desired.stream || {};
  const en = desired.enums || cfg.enums || {};
  const eph = !!(cfg && cfg.drift && cfg.drift.ephemeral);
  streamEnums = en.stream || [];
  curStreamModel = st.model || 'zh-2025';
  const modeSel = $('vlRecMode');
  if (modeSel && document.activeElement !== modeSel && !eph) {
    modeSel.value = st.enabled ? 'stream' : 'vad';
  }
  paintInput('vlStreamSilence', st.endpoint_silence_s, eph);
  fillModelSel(eph);
  applyModeUI();
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
  // 二级模型下拉的离线引擎数据(VAD 模式用);实际填充在 applyStreamFromConfig 里按模式做。
  asrEnums = en.asr || [];
  curAsr = (pair && pair.asr) || '';
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
  applyStreamFromConfig(cfg);
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

// feedId 决定反馈落哪个台:VAD/增益/ASR 属左边 ASR 台(vlAsrFeed),TTS/Vision 属右边
// TTS 台(vlTtsFeed,默认)。之前一律写 vlTtsFeed,导致切 VAD 的提示跑到右边。
async function postConfig(patch, feedNote, feedId = 'vlTtsFeed') {
  if (!invoke) return;
  try {
    await invoke('voice_post', { ip: curIp(), path: '/config', body: JSON.stringify(patch) });
    if (feedNote) addRow(feedId, feedNote, 'ask');
  } catch (e) {
    addRow(feedId, '配置失败: ' + e, 'error');
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

// 流式转写行:partial 实时刷同一行,final(端点)提交为定稿。带「流式」徽章,和下方
// VAD+离线的 seg 行并排,一眼分清是哪条路出的字。用自己的 partialRow,不和别处冲突。
let streamPartialRow = null;
function addStreamRow(ev) {
  const text = ev.text || '';                                   // untrusted model output
  if (ev.partial) {
    if (!streamPartialRow) {
      streamPartialRow = addRow('vlAsrFeed', text, 'stream');
      streamPartialRow.closest('.caprow') &&
        streamPartialRow.closest('.caprow').classList.add('cap-stream', 'cap-live');
    } else {
      streamPartialRow.textContent = text;
    }
  } else {
    if (streamPartialRow) {
      if (text) streamPartialRow.textContent = text;            // 写入定稿文本再提交
      const r = streamPartialRow.closest('.caprow');
      if (r) r.classList.remove('cap-live');                    // 去掉"进行中"样式
      streamPartialRow = null;
    } else if (text) {
      const msg = addRow('vlAsrFeed', text, 'stream');
      msg.closest('.caprow') && msg.closest('.caprow').classList.add('cap-stream');
    }
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
    local.textContent = '💻';
    local.title = '本机播,远程调试用';
    local.onclick = () => playSeg(ev.seg_id, local);
    // 🔁 重识:用当前 ASR 引擎对这段重新识别 —— 切模型后点它,同段同 PCM 只换引擎,
    // 结果并排追加在段下方,直接比模型效果。
    const reasr = document.createElement('button');
    reasr.className = 'minibtn';
    reasr.textContent = '🔁 重识';
    reasr.title = '用当前 ASR 引擎重识别此段,切模型后可并排对比';
    reasr.onclick = () => retranscribeSeg(ev.seg_id, reasr, textwrap);
    textwrap.append(board, local, reasr);
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

// 用当前 ASR 引擎重识别已存段,结果并排追加到该段下方 —— 切引擎后逐段点,
// 就能看到 sensevoice / paraformer 对同一段 PCM 各出什么字。
async function retranscribeSeg(id, btn, wrap) {
  if (!invoke) return;
  btn.disabled = true;
  try {
    const r = JSON.parse(await invoke('voice_post',
      { ip: curIp(), path: '/asr_debug/seg_asr', body: JSON.stringify({ id }) }));
    if (r.error) {
      const msg = /switch in progress/.test(r.error)
        ? '引擎切换中,请等「服务就绪」后再重识' : '重识别失败: ' + r.error;
      addRow('vlAsrFeed', msg, 'error'); return;
    }
    const line = document.createElement('div');
    line.className = 'capreasr';
    const eng = document.createElement('span');
    eng.className = 'reasreng';
    eng.textContent = r.engine || '?';                                // engine id
    const txt = document.createElement('span');
    txt.textContent = (r.text && r.text.trim()) ? r.text : '(空)';    // untrusted ASR text
    line.append(eng, txt);
    wrap.append(line);
  } catch (e) {
    addRow('vlAsrFeed', '重识别失败: ' + e, 'error');
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
      // 流式行(kind 'stream')带「流式」徽章,与 VAD+离线的 seg 行并排区分;
      // seg 行带 outcome+回放;其余(旧 partial/final)走 addAsrEvent。
      if (ev.kind === 'stream') addStreamRow(ev);
      else if (ev.kind === 'seg' || ev.outcome || ev.seg_id != null) addSegRow(ev);
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
    else { stopTail(); partialRow = null; streamPartialRow = null; }
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
      if (asrOn) startTail(); else { stopTail(); partialRow = null; streamPartialRow = null; }
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
  partialRow = null; streamPartialRow = null;
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
$('vlAsrClear') && ($('vlAsrClear').onclick = () => { $('vlAsrFeed').innerHTML = ''; partialRow = null; streamPartialRow = null; });

// 切换是异步 job(载新卸旧数秒)。/health 的 applied.asr 是 daemon 上「真正运行」的
// 引擎(非下拉选值);轮询它直到 == 目标,才算对应模型服务就绪。给出明确确认。
async function confirmAsrSwitch(target) {
  const wait = addRow('vlAsrFeed', '切换中: ' + target + ' …(载入模型数秒)', 'ask');
  for (let i = 0; i < 25; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const h = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/health' }));
      const applied = h.applied && h.applied.asr;
      if (applied === target) {
        if (wait) wait.textContent = '✓ 已切到 ' + target + ' — 服务就绪,可点「🔁 重识」';
        return true;
      }
    } catch { /* transient; keep polling */ }
  }
  if (wait) wait.textContent = '⚠ ' + target + ' 切换未在预期内确认,重识前请再看运行引擎';
  return false;
}

// 流式模型是后台异步加载(可能 700M),POST 立即返回 → 轮询 /health.stream 直到 loaded。
async function confirmStreamSwitch(model) {
  const wait = addRow('vlAsrFeed', '流式加载中: ' + model + ' …(大模型 xlarge 可达 20-30s)', 'ask');
  for (let i = 0; i < 45; i++) {          // xlarge daemon 里 ~24s,留足余量
    await new Promise(r => setTimeout(r, 1000));
    try {
      const h = JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/health' }));
      const st = h.stream || {};
      if (st.enabled && st.loaded && st.model === model) {
        if (wait) wait.textContent = '✓ 流式已就绪: ' + model + ' — 可开始说话';
        return true;
      }
    } catch { /* transient */ }
  }
  if (wait) wait.textContent = '⚠ ' + model + ' 加载未在预期内确认';
  return false;
}

// ASR engine change → ephemeral switch (debug A/B; auto-reverts on leaving DEBUG).
// value 是引擎 id 字符串(apply_axis 的 asr 轴收 str)。先发切换,再轮询确认服务已载。
// 一级:识别模式切换(VAD+离线 ↔ 流式免VAD)。重填二级下拉 + 显隐参数 + ephemeral 切后端。
$('vlRecMode') && ($('vlRecMode').onchange = () => {
  fillModelSel(false);          // 强制按新模式填(不保留旧模式的选择)
  applyModeUI();
  if (recMode() === 'stream') {
    const m = $('vlModelSel').value;
    postConfig({ axis: 'stream', value: curStream(), ephemeral: true },
               '临时切流式(免VAD): ' + m, 'vlAsrFeed');
    confirmStreamSwitch(m);
  } else {
    postConfig({ axis: 'stream', value: { enabled: false }, ephemeral: true },
               '临时回 VAD+离线: ' + $('vlModelSel').value, 'vlAsrFeed');
  }
});
// 二级:模型切换。VAD 模式→切离线引擎(asr 轴);流式模式→切流式模型(stream 轴)。均 ephemeral。
$('vlModelSel') && ($('vlModelSel').onchange = async () => {
  const m = $('vlModelSel').value;
  if (recMode() === 'stream') {
    postConfig({ axis: 'stream', value: curStream(), ephemeral: true },
               '临时切流式模型: ' + m, 'vlAsrFeed');
    confirmStreamSwitch(m);     // 后台加载,轮询到就绪
  } else {
    await postConfig({ axis: 'asr', value: m, ephemeral: true });
    confirmAsrSwitch(m);        // 轮询 /health 直到服务就绪
  }
});

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
             '临时切 VAD: ' + $('vlVadEngine').value, 'vlAsrFeed');
});
for (const id of ['vlVadThreshold', 'vlVadMinSpeech', 'vlVadMinSilence', 'vlVadPreRoll']) {
  $(id) && ($(id).onchange = () => {
    postConfig({ axis: 'vad', value: curVad(), ephemeral: true }, '临时改 VAD 参数', 'vlAsrFeed');
  });
}
$('vlAudioGain') && ($('vlAudioGain').onchange = () => {
  postConfig({ axis: 'audio', value: { gain_db: curGain() }, ephemeral: true },
             '临时增益: ' + curGain() + ' dB', 'vlAsrFeed');
});
// 存(VAD 模式): 落盘 离线引擎(二级) + VAD 引擎/参数 + 增益。
$('vlVadSave') && ($('vlVadSave').onclick = () => {
  postConfig({ axis: 'asr', value: $('vlModelSel').value }, '', 'vlAsrFeed');   // 离线引擎
  postConfig({ axis: 'vad', value: curVad() }, '已存离线引擎+VAD+增益', 'vlAsrFeed');
  postConfig({ axis: 'audio', value: { gain_db: curGain() } });
});

// ---- 流式参数(端点静音临时改;「存」落盘 流式模型+端点+增益,存参) ----------
$('vlStreamSilence') && ($('vlStreamSilence').onchange = () => {
  postConfig({ axis: 'stream', value: curStream(), ephemeral: true },
             '临时改端点静音: ' + curStream().endpoint_silence_s + 's', 'vlAsrFeed');
});
$('vlStreamSave') && ($('vlStreamSave').onclick = () => {
  postConfig({ axis: 'stream', value: curStream() }, '', 'vlAsrFeed');
  postConfig({ axis: 'audio', value: { gain_db: curGain() } }, '已存流式模型+端点+增益', 'vlAsrFeed');
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
