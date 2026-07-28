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
import { $, S, invoke, getCfg, setCfg } from './state.js';

const HEALTH_MS = 1000;   // 1 Hz device telemetry while online
const DBG_HEALTH_MS = 300; // faster telemetry in DEBUG — 1Hz misses 320ms speech peaks
const PROBE_MS  = 2500;   // slow reconnect probe while offline
const TAIL_MS   = 400;    // ASR transcript tail poll, only while transcribing

const sleep = ms => new Promise(r => setTimeout(r, ms));

// dBFS thresholds (from .memory/voice-frontend-s2.md, MCP01 field rules):
//   >= -34  → clearly above this board's noise floor (green)
//   ~ -79   → device muted / not powered (long-press power 3s)
const LVL_MIN  = -80;     // meter floor
// 底噪实测 -38 dBFS(增益已拉满 127/127),留 4dB 余量当「确实有人在说话」。
// 这里曾写作 BARGE_MIN_RMS=0.02 的镜像 —— 那道能量门 2026-07-26 删了(它把
// 6/6 真实语音段都挡在外面),仪表不该再拿一个不存在的门当刻度。
const LVL_HOT  = -34;
const LVL_MUTE = -70;     // at/below this the mic is effectively silent

let active = false, online = false;
let healthTimer = null, tailTimer = null;
let asrOn = false;        // ASR debug transcription is running
// 清空的水位线,落盘。板端 TailRing 是个 200 条的环,GET 时按 since 回放 —— 只清
// DOM 的话,下次开 GUI(或重连)整环原样回来,「清空」就是句谎话。同 agent.js 的
// agentClearedSeq。
let asrClearedSeq = +getCfg('vlAsrClearedSeq', 0) || 0;
let tailSeq = asrClearedSeq;   // /asr_debug/tail cursor
let partialRow = null;    // the single live partial-transcript row (overwritten)
let vadEnums = [];        // enums.vad from GET /config (engine availability + defaults)
let vadFlashUntil = 0;    // wall-clock ms until the "just cut a segment" yellow dot ends
let lastHealth = null;    // last /health payload, so a UI change can repaint at once

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

// edge 需要外网(微软的语音服务)。板子连不上时 daemon 静默回落 Melo,于是换哪个
// edge 音色听起来都一样 —— 表现和「切换不生效」完全一致。熔断状态 /health 一直有,
// 只是没人画。不画出来,这个坑每次都得靠 ssh 上板子 curl 才能定位。
function paintTtsWarn(h) {
  const el = $('vlTtsWarn');
  if (!el) return;
  const isEdge = $('vlTtsEngine') && $('vlTtsEngine').value === 'edge';
  const tripped = !!(online && h && h.edge_breaker);
  el.style.display = (isEdge && tripped) ? '' : 'none';
  if (isEdge && tripped) {
    el.textContent = '⚠ edge 连不上(板子需要外网),已回落本地 Melo —— '
      + '此时换任何 edge 音色都不会有变化。要 edge 的语气,用右下角「电脑播报」(走 Mac 的网)。';
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
  // -99 = daemon isn't capturing (mic closed), so there is no level to show.
  const live = v => (Number.isFinite(v) && v > -99);
  const dbfs = +h.mic_dbfs, peak = +h.mic_peak_dbfs;
  $('vdMicCard').textContent = '麦克风 ' + (cap || '未发现');
  $('vdPlayCard').textContent = '音响 ' + (play || '未发现');
  $('vdMicDbfs').textContent = live(dbfs) ? dbfs.toFixed(0) + ' dBFS' : '—';
  $('vdMicPeak').textContent = '峰 ' + (live(peak) ? peak.toFixed(0) : '—');
  if (fill) {
    const pct = live(dbfs)
      ? Math.max(0, Math.min(100, (dbfs - LVL_MIN) / (0 - LVL_MIN) * 100)) : 0;
    fill.style.width = pct + '%';
    fill.classList.toggle('hot', live(dbfs) && dbfs >= LVL_HOT);
  }
  if (okPill) {
    const ok = h.audio === 'ok';
    okPill.textContent = ok ? '音频就绪' : '音频缺失';
    okPill.className = 'pill ' + (ok ? 'ok' : 'bad');
  }
  paintTtsWarn(h);
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
  paintVadApplicability();   // engine known now: mark the inert boxes
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

// A blank engine or an unparseable number reaches the daemon as "" / null, and
// normalize_vad's job is to never reject a hand-edited file — so it fills the
// gaps from DEFAULT_CONFIG and the save silently downgrades the engine instead
// of failing. Catch it here, where we still know it came from a form.
function vadComplete(v) {
  return !!v.engine && ['threshold', 'min_speech_s', 'min_silence_s', 'pre_roll_s']
    .every(k => Number.isFinite(v[k]));
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

// 参数区随模式显隐(VAD 参数 vs 端点静音);电脑 ASR 那行只在选中 remote 时出现。
function applyModeUI() {
  const stream = recMode() === 'stream';
  if ($('vlVadRow')) $('vlVadRow').style.display = stream ? 'none' : '';
  if ($('vlStreamRow')) $('vlStreamRow').style.display = stream ? '' : 'none';
  const rr = $('vlRemoteRow');
  if (rr) rr.style.display = (!stream && $('vlModelSel') &&
                              $('vlModelSel').value === 'remote') ? '' : 'none';
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

// ---- 电脑 ASR(remote 引擎的地址/模型 + 本机服务的下载与启停) ----------------
// 板端只认地址;下载和起服务是本机(Mac)的事,所以走 Tauri 命令而不是 voice-daemon。
// 没有第二个「位置」开关 —— 二级模型下拉里选中 remote 就是选了电脑 ASR。

function applyRemoteFromConfig(cfg) {
  const desired = (cfg && cfg.desired) || cfg || {};
  const rc = desired.remote_asr || {};
  // 空串不回填:板上没配地址时,不该把用户刚敲进框里还没保存的地址清掉。
  if (rc.url) paintInput('vlRemoteUrl', rc.url, false);
  paintInput('vlRemoteModel', rc.model, false);
}

function curRemote() {
  return { url: ($('vlRemoteUrl') && $('vlRemoteUrl').value.trim()) || '',
           model: ($('vlRemoteModel') && $('vlRemoteModel').value.trim()) || '' };
}

// 本机服务状态:模型下了多少、服务在不在、这台机器的局域网地址是什么。
let remoteStatTimer = null;
async function pollRemoteStatus() {
  const el = $('vlRemoteStat');
  if (!el || !invoke) return;
  const rr = $('vlRemoteRow');
  if (!rr || rr.style.display === 'none') return;      // 不看的时候不查
  try {
    const s = JSON.parse(await invoke('mac_asr_status', { model: curRemote().model }));
    const parts = [s.running ? '服务在跑' : '服务未启动',
                   s.cached_mb > 0 ? `模型已下载 ${s.cached_mb}MB` : '模型未下载'];
    if (s.lan_ip) parts.push(`本机 http://${s.lan_ip}:${s.port}`);
    el.textContent = '状态 ' + parts.join(' · ');
    // 地址空着就把本机地址填进去 —— 这是唯一正确的候选值,让人手抄 IP 是浪费
    const u = $('vlRemoteUrl');
    if (u && !u.value.trim() && s.lan_ip) u.value = `http://${s.lan_ip}:${s.port}`;
  } catch (e) {
    el.textContent = '状态读取失败: ' + e;
  }
}

$('vlRemoteDl') && ($('vlRemoteDl').onclick = async () => {
  const btn = $('vlRemoteDl');
  const model = curRemote().model;
  if (!model) { addRow('vlAsrFeed', '先填模型仓库 id', 'error'); return; }
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '下载中…';
  addRow('vlAsrFeed', `下载 ${model} 到本机 HF 缓存(首次约几分钟,不要关 GUI)`, 'ask');
  try {
    await invoke('mac_asr_download', { model });
    addRow('vlAsrFeed', '模型已就绪: ' + model, 'answer');
  } catch (e) {
    addRow('vlAsrFeed', '下载失败: ' + e, 'error');
  } finally {
    btn.textContent = old;
    btn.disabled = false;
    pollRemoteStatus();
  }
});

$('vlRemoteSrv') && ($('vlRemoteSrv').onclick = async () => {
  const btn = $('vlRemoteSrv');
  const start = btn.textContent.indexOf('停') < 0;
  btn.disabled = true;
  try {
    const out = await invoke('mac_asr_serve',
                             { action: start ? 'start' : 'stop', model: curRemote().model });
    addRow('vlAsrFeed', start ? ('本机 ASR 服务已启动,日志 ' + out) : '本机 ASR 服务已停止',
           'answer');
    btn.textContent = start ? '停止服务' : '启动服务';
  } catch (e) {
    addRow('vlAsrFeed', (start ? '启动失败: ' : '停止失败: ') + e, 'error');
  } finally {
    btn.disabled = false;
    setTimeout(pollRemoteStatus, 1200);   // uv 冷启动要一会儿才 listen
  }
});

// 地址/模型改动 → 立刻落盘(它不是引擎切换,不抢切换锁)。remote 正在当值时
// daemon 会顺手复探,地址打错当场 502。
async function saveRemote(note) {
  return postConfig({ axis: 'remote_asr', value: curRemote() }, note, 'vlAsrFeed');
}
for (const id of ['vlRemoteUrl', 'vlRemoteModel']) {
  const el = $(id);
  if (el) el.onchange = () => { saveRemote('已保存电脑 ASR 地址/模型'); pollRemoteStatus(); };
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
  applyRemoteFromConfig(cfg);
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

// 每个下拉的选项都来自这里。它失败时旧代码整条吞掉,页面就剩一排空下拉 —— 看起来
// 和「代码写坏了」一模一样,实际是板子答不上来(内存被挤爆时 /config 直接超时,
// 或刚重启还没 bind 端口)。所以:重试几次,仍失败就把原因写进转写台。
async function refreshConfig(tries = 3) {
  if (!invoke) return;
  for (let i = 1; i <= tries; i++) {
    try {
      applyTtsFromConfig(JSON.parse(await invoke('voice_get', { ip: curIp(), path: '/config' })));
      return;
    } catch (e) {
      if (i === tries) {
        addRow('vlAsrFeed', '读取板端配置失败,下拉框留空(不是配置丢了): ' + e, 'error');
        return;
      }
      await sleep(600 * i);
    }
  }
}

// feedId 决定反馈落哪个台:VAD/增益/ASR 属左边 ASR 台(vlAsrFeed),TTS/Vision 属右边
// TTS 台(vlTtsFeed,默认)。之前一律写 vlTtsFeed,导致切 VAD 的提示跑到右边。
// asr/vad are engine switches serialised behind one daemon-side lock, and the
// endpoint answers 202 the moment the job is QUEUED — so awaiting the response
// tells you nothing about when the lock frees. Anything sent meanwhile bounces
// off a 409 "switch in progress". Retry on exactly that, with a backoff long
// enough for a real load (funasr ~3s, qwen3 ~8s).
async function postConfig(patch, feedNote, feedId = 'vlTtsFeed', tries = 8) {
  if (!invoke) return false;
  for (let i = 0; i < tries; i++) {
    try {
      await invoke('voice_post', { ip: curIp(), path: '/config', body: JSON.stringify(patch) });
      if (feedNote) addRow(feedId, feedNote, 'ask');
      return true;
    } catch (e) {
      if (!String(e).includes('switch in progress') || i === tries - 1) {
        addRow(feedId, '配置失败: ' + e, 'error');
        return false;
      }
      await new Promise(r => setTimeout(r, 500 * (i + 1)));
    }
  }
  return false;
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
  too_short: '过短', echo: '回声',
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
    // 水位线只在**同一个 daemon 进程内**有意义,靠环自己报的两个边界校正,不看时钟:
    //   cursor > last_seq  → daemon 重启过,seq 从头编号,旧水位线指向不相干的事件
    //   cursor < oldest-1  → 环转过去了(200 条),再按旧 cursor 拉只会漏,不会补
    // 少了后一条,「GUI 关着的时候 daemon 重启并涨过了水位线」会把整段转写吞掉,
    // 表现和 2026-07-26 那次一模一样:板子认对了,台上一片空白。
    if (r.last_seq != null && r.last_seq < tailSeq) {
      tailSeq = 0;
      if (asrClearedSeq) { asrClearedSeq = 0; setCfg('vlAsrClearedSeq', 0); }
      return;
    }
    if (r.oldest_seq > 0 && tailSeq < r.oldest_seq - 1) {
      tailSeq = r.oldest_seq - 1;
      if (asrClearedSeq > tailSeq) { asrClearedSeq = tailSeq; setCfg('vlAsrClearedSeq', tailSeq); }
    }
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
  if (tailTimer) return;      // idempotent: the health loop calls this every tick
  tailTimer = setInterval(pollTail, TAIL_MS);
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
    // 开台 = 「显示自上次清空以来的全部转写」。从水位线重放,所以先清 DOM,否则
    // 关了再开会把屏幕上已有的行再追加一遍。板端不再清环(见 daemon.set_bench),
    // 清空这件事只有这一个机制:asrClearedSeq。
    if (on) {
      $('vlAsrFeed').innerHTML = '';
      partialRow = null; streamPartialRow = null;
      tailSeq = asrClearedSeq;
      startTail();
    }
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
    lastHealth = h;
    if (!online) goOnline();
    paintDevice(h);
    // The daemon owns the bench switch (another window / a restart may flip it):
    // mirror it instead of trusting our local toggle. It is its own field now,
    // not a value of `state` — the bench and the conversation run side by side,
    // so "is the bench on" and "what is the conversation doing" are two answers.
    const debug = !!h.bench;
    if (debug !== asrOn) {
      asrOn = debug;
      paintAsrBtn();
      armHealth();
      if (!asrOn) { partialRow = null; streamPartialRow = null; }
    }
    // The tail timer is DERIVED state — reconcile it every poll, do not toggle it
    // on the edge above. It used to start only when `asrOn` CHANGED, and leaving
    // the Voice tab clears the timer (stopActive) while leaving `asrOn` true. So
    // coming back found debug === asrOn, took no edge, and never restarted the
    // timer: the bench polled nothing for the rest of the session while the board
    // kept producing rows. Observed as tail seq climbing 1 -> 22 on the daemon
    // with the GUI frozen at 1, and it survives any number of tab switches.
    if (asrOn) startTail(); else stopTail();
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
  macInit();                                    // say -v ? once, on first visit
  if (!remoteStatTimer) remoteStatTimer = setInterval(pollRemoteStatus, 3000);
  pollRemoteStatus();
}

function stopActive() {
  if (!active) return;
  active = false;
  stopTail();
  if (remoteStatTimer) { clearInterval(remoteStatTimer); remoteStatTimer = null; }
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = null;
  online = false;
  paintDevice(null);
}

export function onEnterVoice() { startActive(); refreshVoiceSvcAuto(); }
export function onLeaveVoice() { stopActive(); }

// ---- wiring --------------------------------------------------------------

$('vlAsrBtn') && ($('vlAsrBtn').onclick = () => { if (online) setAsr(!asrOn); });
$('vlAsrClear') && ($('vlAsrClear').onclick = () => {
  $('vlAsrFeed').innerHTML = '';
  partialRow = null; streamPartialRow = null;
  asrClearedSeq = tailSeq;                       // 记住清到哪,否则重连全回来
  setCfg('vlAsrClearedSeq', asrClearedSeq);
});

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
  applyModeUI();                // 电脑 ASR 那行随选择出现/消失
  if (recMode() === 'stream') {
    postConfig({ axis: 'stream', value: curStream(), ephemeral: true },
               '临时切流式模型: ' + m, 'vlAsrFeed');
    confirmStreamSwitch(m);     // 后台加载,轮询到就绪
  } else {
    // 切到电脑 ASR 前先把地址存下:切换会当场探活,探的必须是框里这个地址,
    // 而不是上次保存的那个 —— 否则「改了地址再切」永远探错目标。
    if (m === 'remote') { await saveRemote(''); pollRemoteStatus(); }
    await postConfig({ axis: 'asr', value: m, ephemeral: true });
    confirmAsrSwitch(m);        // 轮询 /health 直到服务就绪
  }
});

// TTS engine / voice change → ephemeral override (does NOT persist).
// POST /config body shape is {axis, value, ephemeral} — by-axis whole replacement.
$('vlTtsEngine') && ($('vlTtsEngine').onchange = () => {
  syncTtsUi();
  paintTtsWarn(lastHealth);      // 切到/切走 edge 时提示要立刻跟上,不等下一次轮询
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

// Engines differ in which knobs they actually read. fsmn ignores threshold and
// min_silence entirely (it runs its own state machine — see FsmnVad's docstring),
// so leaving those boxes looking live invites tuning a value that does nothing.
// Dimmed, not disabled: the value still persists for whichever engine does use it.
const VAD_IGNORED = { fsmn: ['vlVadThreshold', 'vlVadMinSilence'] };

function paintVadApplicability() {
  const eng = $('vlVadEngine') && $('vlVadEngine').value;
  const dead = VAD_IGNORED[eng] || [];
  for (const id of ['vlVadThreshold', 'vlVadMinSpeech', 'vlVadMinSilence', 'vlVadPreRoll']) {
    const el = $(id);
    if (!el || !el.parentElement) continue;
    const off = dead.includes(id);
    el.parentElement.classList.toggle('vadoff', off);
    el.parentElement.title = off
      ? `${eng} 引擎忽略此项，改了不会有任何效果`
      : (el.parentElement.dataset.tip || el.parentElement.title);
  }
}

// ---- VAD engine / params + digital gain (global audio front-end) ----------
// All changes are ephemeral (debug override, auto-reverts on leaving DEBUG); the
// small 「存」 button is the only thing that persists. VAD is NOT part of a preset
// pair — it is the global front-end, so it gets its own save, not the pair's.
$('vlVadEngine') && ($('vlVadEngine').onchange = () => {
  const e = vadEnums.find(x => x.id === $('vlVadEngine').value);
  if (e && e.default_threshold != null) $('vlVadThreshold').value = e.default_threshold;
  postConfig({ axis: 'vad', value: curVad(), ephemeral: true },
             '临时切 VAD: ' + $('vlVadEngine').value, 'vlAsrFeed');
  paintVadApplicability();
});
for (const id of ['vlVadThreshold', 'vlVadMinSpeech', 'vlVadMinSilence', 'vlVadPreRoll']) {
  $(id) && ($(id).onchange = () => {
    const v = curVad();
    // An emptied box parses to NaN, ships as null, and comes back as a default.
    if (!vadComplete(v)) { addRow('vlAsrFeed', 'VAD 参数为空,未提交', 'error'); return; }
    postConfig({ axis: 'vad', value: v, ephemeral: true }, '临时改 VAD 参数', 'vlAsrFeed');
  });
}
$('vlAudioGain') && ($('vlAudioGain').onchange = () => {
  postConfig({ axis: 'audio', value: { gain_db: curGain() }, ephemeral: true },
             '临时增益: ' + curGain() + ' dB', 'vlAsrFeed');
});
// 保存(唯一按钮,一级行): 按当前识别模式落盘整套识别配置。识别模式本身也持久化
// (stream.enabled) —— 否则存过流式后切回 VAD,重开永远回到流式。
let savingAll = false;
$('vlSaveAll') && ($('vlSaveAll').onclick = async () => {
  // Sequential, not parallel: the four axes share one switch lock, so firing
  // them together made two of them 409 and the save half-applied without saying
  // so. Re-entry is blocked with a flag rather than the native `disabled`
  // attribute — that swallows the click outright (see .memory/gui-disabled-*).
  if (savingAll) return;
  savingAll = true;
  try {
    if (recMode() === 'stream') {
      await postConfig({ axis: 'stream', value: curStream() }, '', 'vlAsrFeed');
      await postConfig({ axis: 'audio', value: { gain_db: curGain() } },
                       '已保存 流式模式+模型+端点+增益', 'vlAsrFeed');
      return;
    }
    const vad = curVad();
    if (!vadComplete(vad)) {
      addRow('vlAsrFeed', '保存中止: VAD 引擎或参数为空,先等配置载入', 'error');
      return;
    }
    // 电脑 ASR:地址先落盘,再切引擎 —— 反过来的话引擎会拿旧地址去探活。
    if ($('vlModelSel').value === 'remote' && !await saveRemote('')) {
      addRow('vlAsrFeed', '保存中止: 电脑 ASR 地址无效或不可达', 'error');
      return;
    }
    const ok = await postConfig({ axis: 'asr', value: $('vlModelSel').value }, '', 'vlAsrFeed')
      && await postConfig({ axis: 'vad', value: vad }, '', 'vlAsrFeed')
      && await postConfig({ axis: 'audio', value: { gain_db: curGain() } }, '', 'vlAsrFeed')
      && await postConfig({ axis: 'stream', value: { enabled: false } }, '', 'vlAsrFeed');
    addRow('vlAsrFeed', ok ? '已保存 VAD模式+离线引擎+参数+增益' : '保存未完成,见上方错误',
           ok ? 'ask' : 'error');
  } finally {
    savingAll = false;
  }
});

// ---- 流式参数(端点静音临时改;「存」落盘 流式模型+端点+增益,存参) ----------
$('vlStreamSilence') && ($('vlStreamSilence').onchange = () => {
  postConfig({ axis: 'stream', value: curStream(), ephemeral: true },
             '临时改端点静音: ' + curStream().endpoint_silence_s + 's', 'vlAsrFeed');
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

// ---- voice-daemon service control (moved here from the Agent page) ---------
// These are operations, not conversation: the Agent page is for talking to the
// robot, so the IP field and the start/stop buttons live on this page instead.
// Feedback goes to the ASR bench feed — the same page the buttons are on.
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
    addRow('vlAsrFeed', `${label}语音服务…`, 'ask');
    try {
      await invoke('voice_service', { ip: curIp(), action });
      addRow('vlAsrFeed', `服务${label}完成`, 'ask');
    } catch (e) {
      addRow('vlAsrFeed', `服务${label}失败: ${e}`, 'error');
    } finally {
      btn.disabled = false;
    }
  };
}

async function refreshVoiceSvcAuto() {
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
    addRow('vlAsrFeed', `开机自启已${cb.checked ? '开启' : '关闭'}`, 'ask');
  } catch (err) {
    addRow('vlAsrFeed', `开机自启设置失败: ${err}`, 'error');
    cb.checked = !cb.checked;
  } finally {
    cb.disabled = false;
  }
});

// ---- 电脑播报 (Mac speaker) ----------------------------------------------
// This Mac speaks; the robot listens. Deliberately NOT gated on `online` and it
// never calls voice-daemon — the whole value is a sound source the board did not
// produce (VAD/ASR tuning, echo tests, wake drills). Rust owns the `say` child;
// this side owns the script, the gaps and the stop flag.
let macRunning = false;    // a script is playing right now
let macStopped = false;    // 停止 pressed — ends the line, the gap and the loop
let macVoicesLoaded = false;


// Same full-width-dot rule as numVal, plus a default for empty input.
function macNum(id, dflt) {
  const v = parseFloat((($(id) && $(id).value) || '').replace(/[。，]/g, '.').trim());
  return Number.isFinite(v) ? v : dflt;
}

// One line = one sentence. Blank lines and #-comments are skipped so a script
// can be annotated in place.
//
// Voice and gap come from the controls above and apply
// to the whole script — a per-line override was two extra namespaces to keep
// straight (say's voice names are not edge's, and a stray "| 1" read as a gap)
// for a knob nobody turned. Anything after the first "|" is dropped, so scripts
// saved in the old `文本 | 音色 | 停顿` form still load instead of speaking their
// own column separators out loud.
function macParse() {
  const out = [];
  for (const raw of (($('vlMacScript') && $('vlMacScript').value) || '').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const text = line.split('|')[0].trim();
    if (text) out.push(text);
  }
  return out;
}

function paintMacBtns() {
  const b = $('vlMacBtn'), s = $('vlMacStop');
  if (b) { b.disabled = macRunning || !invoke; b.textContent = macRunning ? '播报中…' : '开始播报'; }
  if (s) s.disabled = !macRunning;
}

// edge/f5 are rendered to files BEFORE the first line plays. Synthesis latency
// must not leak into the gaps: the gap is the measured variable in a VAD run
// (0.8-10s, deliberately unequal), and f5 costs 1.4-2.6s per line. Rendering
// up front also loads f5's model once instead of once per line.
// Returns text -> file path, or null if the user stopped / it failed.
async function macRender(lines, engine, voice) {
  const seed = 1234;                    // fixed: same script -> same audio, always
  const uniq = [...new Set(lines)];
  const items = uniq.map(text => ({ text, key: macKey(engine, voice, seed, text) }));
  addRow('vlMacFeed', `生成中: ${engine} / ${voice} / ${uniq.length} 句…`, 'ask');
  let res;
  try {
    res = JSON.parse(await invoke('mac_tts_render', { engine, voice, seed, items }));
  } catch (e) {
    addRow('vlMacFeed', '生成失败: ' + e, 'error');
    return null;
  }
  if (macStopped) return null;
  addRow('vlMacFeed', `就绪: 新生成 ${res.rendered} 句, 命中缓存 ${res.cached} 句`, 'answer');
  return new Map(uniq.map((text, i) => [text, res.paths[i]]));
}

async function macRun() {
  if (macRunning || !invoke) return;
  const lines = macParse();
  if (!lines.length) { addRow('vlMacFeed', '脚本是空的(每行一句话)', 'error'); return; }
  const engine = macEngine();
  const volume = Math.max(0, Math.min(100, Math.round(macNum('vlMacVol', 100))));
  const rate = Math.max(0, Math.round(macNum('vlMacRate', 0)));
  const gap = Math.max(0, macNum('vlMacGap', 3));
  macRunning = true;
  macStopped = false;
  paintMacBtns();
  try {
    const voice = ($('vlMacVoice') && $('vlMacVoice').value) || '';
    let rendered = null;
    if (engine !== 'say') {
      rendered = await macRender(lines, engine, voice);
      if (!rendered) return;            // stopped or failed; macRender already said why
    }
    do {
      for (const text of lines) {
        if (macStopped) break;
        addRow('vlMacFeed', '▶ ' + text, 'answer');
        try {
          if (rendered) {
            await invoke('mac_play', { path: rendered.get(text), volume });
          } else {
            await invoke('mac_say', { text, voice, volume, rate });
          }
        } catch (e) {
          addRow('vlMacFeed', '播报失败: ' + e, 'error');
          macStopped = true;
          break;
        }
        // Sliced so 停止 cuts the gap too — a 10s pause must not outlive the button.
        for (let left = gap * 1000; left > 0 && !macStopped; left -= 100) {
          await sleep(Math.min(100, left));
        }
      }
    } while (!macStopped && $('vlMacLoop') && $('vlMacLoop').checked);
  } finally {
    addRow('vlMacFeed', macStopped ? '已停止' : '播报结束', 'ask');
    macRunning = false;
    paintMacBtns();
  }
}

async function macStop() {
  macStopped = true;
  try { await invoke('mac_say_stop'); } catch { /* nothing was speaking */ }
}

// edge/f5 share one voice namespace on purpose: f5 clones an edge voice, so the
// same name means the same sound and the engine only decides online-every-time
// vs clone-once-then-offline. Mirrors voice_config.EDGE_VOICES.
const EDGE_VOICES = [
  'zh-CN-XiaoxiaoNeural', 'zh-CN-XiaoyiNeural', 'zh-CN-YunxiNeural',
  'zh-CN-YunyangNeural', 'zh-CN-YunjianNeural', 'zh-CN-YunxiaNeural',
  'zh-CN-liaoning-XiaobeiNeural', 'zh-CN-shaanxi-XiaoniNeural',
  'zh-HK-HiuMaanNeural', 'zh-TW-HsiaoChenNeural',
];
let sayVoices = [];        // "name\tlocale" rows from `say -v ?`, loaded once

function macEngine() { return ($('vlMacEngine') && $('vlMacEngine').value) || 'edge'; }

// Voice dropdown follows the engine; 语速 only exists for say.
function fillMacVoices() {
  const sel = $('vlMacVoice');
  if (!sel) return;
  const say = macEngine() === 'say';
  const want = sel.value || getCfg(say ? 'macVoiceSay' : 'macVoice',
                                  say ? 'Tingting' : EDGE_VOICES[0]);
  sel.innerHTML = '';
  const rows = say ? sayVoices.map(r => r.split('\t')) : EDGE_VOICES.map(v => [v, '']);
  for (const [name, locale] of rows) {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = locale ? name + '  (' + locale + ')' : name;
    sel.append(o);
  }
  if ([...sel.options].some(o => o.value === want)) sel.value = want;
  const rw = $('vlMacRateWrap');
  if (rw) rw.style.display = say ? '' : 'none';
}

// `say -v ?` once, on first visit to the page — not at app boot, where nobody is
// looking at this panel.
async function macInit() {
  if (macVoicesLoaded || !invoke) return;
  macVoicesLoaded = true;
  try {
    sayVoices = await invoke('mac_voices');
  } catch (e) {
    addRow('vlMacFeed', '读取系统音色失败: ' + e, 'error');
  }
  fillMacVoices();
}

// Cache key over everything that changes the audio. FNV-1a in two 32-bit halves:
// a collision would play the wrong cached line, so 64 bits, not 32.
//
// RENDER_REV is part of the key because the renderer's post-processing is too:
// r2 added peak normalisation, and without a bump every already-cached line
// would keep playing at the old, quieter level and the fix would look inert.
// Bump it whenever scripts/mac_tts_render.py changes the samples it writes.
const RENDER_REV = 'r2';

function macKey(engine, voice, seed, text) {
  const s = RENDER_REV + '|' + engine + '|' + voice + '|' + seed + '|' + text;
  let h1 = 0x811c9dc5, h2 = 0x01000193;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
    h2 = Math.imul(h2 ^ (c + i), 0x85ebca6b) >>> 0;
  }
  return h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0');
}

// Persistence rides the one config file (state.js getCfg/setCfg) — same rule as
// everything else here: no localStorage second source of truth.
const MAC_FIELDS = { vlMacEngine: 'macEngine', vlMacVol: 'macVolume',
                     vlMacRate: 'macRate', vlMacGap: 'macGap',
                     vlMacScript: 'macScript' };
for (const [id, key] of Object.entries(MAC_FIELDS)) {
  const el = $(id);
  if (!el) continue;
  const saved = getCfg(key, null);
  if (saved !== null && saved !== undefined) el.value = saved;
  el.addEventListener('change', () => setCfg(key, el.value));
}
// Scripts saved in the old `文本 | 音色 | 停顿` form: macParse ignores the columns
// anyway, but leaving them in the box shows the operator a format that no longer
// means anything. Rewrite once, on load. Only the separators go — every line's
// text is untouched, so this cannot lose anything the user typed.
if ($('vlMacScript') && $('vlMacScript').value.includes('|')) {
  $('vlMacScript').value = $('vlMacScript').value.split('\n')
    .map(l => l.split('|')[0].replace(/\s+$/, '')).join('\n');
  setCfg('macScript', $('vlMacScript').value);
}
$('vlMacLoop') && ($('vlMacLoop').checked = !!getCfg('macLoop', false));
$('vlMacLoop') && ($('vlMacLoop').onchange = e => setCfg('macLoop', e.target.checked));
// The voice is remembered per engine — say's names and edge's names are
// different namespaces, and one shared slot would keep clobbering the other.
$('vlMacVoice') && ($('vlMacVoice').onchange = () =>
  setCfg(macEngine() === 'say' ? 'macVoiceSay' : 'macVoice', $('vlMacVoice').value));
$('vlMacEngine') && ($('vlMacEngine').addEventListener('change', fillMacVoices));

// The two volumes multiply and only one of them lives in this window, so show
// the product: a bench at 100% behind a system volume of 69 is 3dB down, and
// 8dB is the whole difference between 3/10 and 9/10 lines recognised.
async function paintSysVol() {
  const el = $('vlMacSysVol');
  if (!el) return;
  let sys;
  try { sys = await invoke('mac_sysvol_get'); }
  catch (e) { el.textContent = '(系统音量读不到)'; return; }
  const raw = $('vlMacVol') ? numVal('vlMacVol') : 100;
  const bench = Math.max(0, Math.min(100, isFinite(raw) ? raw : 100));
  const eff = Math.round(sys * bench / 100);
  el.textContent = `×系统 ${sys}% = ${eff}%`;
  el.classList.toggle('vlwarn', eff < 95);
}
// "拉满" means the thing the operator is looking at — the product. Maxing only
// the system volume looked broken the one time it was already 100 and the bench
// sat at 70: the readout said 70% and the button did nothing visible.
$('vlMacSysMax') && ($('vlMacSysMax').onclick = async () => {
  const v = $('vlMacVol');
  if (v) { v.value = '100'; setCfg('macVolume', '100'); }   // programmatic set fires no 'change'
  try { await invoke('mac_sysvol_set', { volume: 100 }); }
  catch (e) { addRow('vlMacFeed', '设置系统音量失败: ' + e, 'error'); }
  paintSysVol();
});
$('vlMacVol') && ($('vlMacVol').addEventListener('input', paintSysVol));
paintSysVol();
$('vlMacBtn') && ($('vlMacBtn').onclick = macRun);
$('vlMacStop') && ($('vlMacStop').onclick = macStop);
$('vlMacClear') && ($('vlMacClear').onclick = () => { $('vlMacFeed').innerHTML = ''; });
// The built-in script lives in index.html's textarea, so the default IS the
// markup — keep a copy at load time rather than duplicating it here, or the two
// drift apart the first time one of them is edited.
const MAC_DEFAULT_SCRIPT = ($('vlMacScript') && $('vlMacScript').defaultValue) || '';
$('vlMacReset') && ($('vlMacReset').onclick = () => {
  const el = $('vlMacScript');
  if (!el) return;
  el.value = MAC_DEFAULT_SCRIPT;
  setCfg('macScript', el.value);
  addRow('vlMacFeed', '已恢复默认脚本(14 句调参语料)', 'ask');
});
paintMacBtns();
