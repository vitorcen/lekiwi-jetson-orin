// Leader-arm teleoperation: a local SO-101 leader on a USB serial port drives
// the follower arm on the robot.
//
// The Rust backend owns the serial port and the 30 Hz follow loop (deltas from
// the aligned zero pose go to base_host as {"arm.dq": [...]}); this module is
// only the control surface: connect / align / follow buttons and a live joint
// readout fed by "leader" events. Deliberately not tied to keyboard focus —
// the operator's grip on the physical leader arm is the dead-man here, and
// base_host clamps every step.
import { $, invoke } from './state.js';
import { logLine } from './log.js';

let connected = false;
let aligned = false;
let following = false;
let calTarget = null;          // 'leader' | 'follower'
let calStage = null;           // 'middle' | 'range'
let followerPollToken = 0;
let renderedControls = '';

function setState(text, cls) {
  const el = $('lstate');
  el.textContent = text;
  el.className = 'pill ' + cls;
}

// A native `disabled` button swallows the click whole — no handler, no error,
// no log line. That turned "跟随点了没反应" into an unfalsifiable bug: the UI
// looked ready while the click went nowhere. Keep the buttons clickable and
// let the handler say WHY it refused; the backend re-checks anyway
// (leader_follow only arms when a zero pose exists).
// Joint samples arrive around 30 Hz. Rewriting a native button's text while it
// is pressed makes WKWebView cancel the final click after mouseup. Explicit
// actions force a repaint; telemetry only repaints when control state changed.
function refresh(force = true) {
  const state = `${connected}|${aligned}|${following}`;
  if (!force && state === renderedControls) return;
  renderedControls = state;
  $('lconn').textContent = connected ? '断开主臂' : '连接主臂';
  $('lalign').setAttribute('aria-disabled', String(!connected));
  $('lfollow').setAttribute('aria-disabled', String(!connected || !aligned));
  $('lcal').setAttribute('aria-disabled', String(!connected));
  $('lfollow').textContent = following ? '停止跟随' : '开始跟随';
  $('lfollow').classList.toggle('primary', connected && aligned && !following);
  $('lfollow').classList.toggle('live', following);
  if (!connected) setState('未连接', 'bad');
  else if (following) setState('跟随中', 'ok');
  else if (aligned) setState('已对齐，未跟随', 'warn');
  else setState('已连接，未对齐', 'warn');
}

async function connectLeader(quiet) {
  setState('连接中…', 'warn');
  try {
    await invoke('leader_connect', { path: $('lport').value.trim() });
    connected = true;
    logLine('主臂', '已连接');
    refresh();
  } catch (e) {
    if (quiet) refresh();               // no leader plugged in: stay calm
    else { setState('连接失败: ' + e, 'bad'); logLine('主臂', '连接失败: ' + e); }
  }
}

$('lconn').onclick = async () => {
  if (!invoke) return;
  if (connected) {
    await invoke('leader_disconnect').catch(() => {});
    connected = aligned = following = false;
    if (calTarget === 'leader') closeCalibration();
    refresh();
    return;
  }
  await connectLeader(false);
};

// Glide the follower to its calibrated middle pose (needs ZMQ connected).
$('lmid').onclick = () => {
  if (calTarget) { logLine('校准', '校准进行中，不能自动移动从臂'); return; }
  if (invoke) invoke('zmq_arm_mid').catch(() => {});
  logLine('主臂', '从臂摆中位');
};

$('lalign').onclick = async () => {
  if (!invoke) return;
  if (!connected) { logLine('主臂', '无法对齐：主臂未连接'); return; }
  try {
    await invoke('leader_align');
    aligned = true;
    logLine('主臂', '已对齐零位');
    refresh();
  } catch (e) {
    setState('对齐失败: ' + e, 'bad');
    logLine('主臂', '对齐失败: ' + e);
  }
};

$('lfollow').onclick = async () => {
  if (!invoke) return;
  if (calTarget) {
    setState('校准进行中，不能跟随', 'bad');
    logLine('主臂', '校准进行中，不能开始跟随');
    return;
  }
  if (!connected) {
    setState('无法跟随：主臂未连接', 'bad');
    logLine('主臂', '无法跟随：主臂未连接');
    return;
  }
  if (!aligned) {
    setState('无法跟随：未对齐零位', 'bad');
    logLine('主臂', '无法跟随：未对齐零位（先摆中位再点「对齐零位」）');
    return;
  }
  const on = !following;
  try {
    following = await invoke('leader_follow', { on });
  } catch (e) {
    setState('跟随命令失败: ' + e, 'bad');
    logLine('主臂', '跟随命令失败: ' + e);
    return;                            // state stays as the backend last reported
  }
  // Stopping follow parks the follower: fold to rest, cut torque.
  if (!following) invoke('zmq_arm_relax').catch(() => {});
  logLine('主臂', following ? '开始跟随' : '停止跟随 → 收臂松弛');
  refresh();
};

// Anytime button: fold the follower to rest and go limp (gamepad START twin).
$('lrelax').onclick = () => {
  if (!invoke) return;
  if (following) {
    following = false;
    invoke('leader_follow', { on: false }).catch(() => {});
    refresh();
  }
  invoke('zmq_arm_relax').catch(() => {});
  logLine('主臂', '收臂松弛');
};

function closeCalibration() {
  calTarget = calStage = null;
  followerPollToken++;
  $('calbox').hidden = true;
}

function paintCalibration() {
  $('calbox').hidden = false;
  $('caltarget').textContent = calTarget === 'leader' ? '主臂校准' : '从臂校准';
  if (calStage === 'middle') {
    $('calstep').textContent = '1 / 2';
    $('caltext').textContent =
      '扭矩已切断。手动把机械臂摆到关节行程的中位：大臂竖直、小臂水平、腕部伸直，夹爪半开。确认后记录中位。';
    $('calnext').textContent = '记录中位，开始采集范围';
  } else {
    $('calstep').textContent = '2 / 2';
    $('caltext').textContent =
      '依次把关节 1–6 缓慢摆到两个最大允许位置。第 5 关节也只摆到安全端点，不要强行绕过线缆或机械限制。每个关节都必须覆盖两端，然后完成保存。';
    $('calnext').textContent = '完成并保存校准';
  }
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function followerAction(action, expected) {
  const seq = Date.now();
  await invoke('zmq_arm_calibrate', { action, seq });
  const ip = $('ip').value.trim();
  if (!ip) throw new Error('Orin IP 为空');
  for (let i = 0; i < 16; i++) {
    await wait(250);
    const status = await invoke('arm_cal_status', { ip });
    if (status.startsWith(`${seq} error `)) {
      const message = status.slice(`${seq} error `.length).split(' joints=')[0];
      throw new Error(message);
    }
    if (status.startsWith(`${seq} ${expected}`)) return status;
  }
  throw new Error('板端校准状态未更新，请确认 ZeroMQ 已连接');
}

function paintFollowerJoints(status) {
  const match = status.match(/\bjoints=([0-9,]+)/);
  if (!match) return;
  const joints = match[1].split(',');
  for (let i = 0; i < 6; i++) $('lj' + i).textContent = joints[i] || '—';
}

async function pollFollowerJoints() {
  const token = ++followerPollToken;
  const ip = $('ip').value.trim();
  while (calTarget === 'follower' && token === followerPollToken) {
    try {
      paintFollowerJoints(await invoke('arm_cal_status', { ip }));
    } catch { /* action polling reports connection failures */ }
    await wait(250);
  }
}

async function startCalibration(target) {
  if (!invoke || calTarget) return;
  if (target === 'leader' && !connected) {
    logLine('校准', '主臂未连接');
    return;
  }
  try {
    if (following) {
      await invoke('leader_follow', { on: false });
      following = false;
    }
    if (target === 'leader') await invoke('leader_calibrate', { action: 'start' });
    else await followerAction('start', 'middle');
    calTarget = target;
    calStage = 'middle';
    paintCalibration();
    if (target === 'follower') pollFollowerJoints();
    logLine('校准', `${target === 'leader' ? '主臂' : '从臂'}：请摆到中位`);
    refresh();
  } catch (e) {
    logLine('校准', '启动失败: ' + e);
  }
}

$('lcal').onclick = () => startCalibration('leader');
$('fcal').onclick = () => startCalibration('follower');

$('calnext').onclick = async () => {
  if (!calTarget) return;
  try {
    if (calStage === 'middle') {
      if (calTarget === 'leader') {
        await invoke('leader_calibrate', { action: 'middle' });
      } else {
        await followerAction('middle', 'range');
      }
      calStage = 'range';
      paintCalibration();
      logLine('校准', '正在采集各关节最大允许范围');
    } else {
      if (calTarget === 'leader') {
        await invoke('leader_calibrate', { action: 'finish' });
        aligned = true;              // calibration makes the middle exactly 2047
      } else {
        await followerAction('finish', 'saved');
      }
      logLine('校准', `${calTarget === 'leader' ? '主臂' : '从臂'}校准已保存`);
      closeCalibration();
      refresh();
    }
  } catch (e) {
    logLine('校准', '未完成: ' + e);
  }
};

$('calcancel').onclick = async () => {
  if (!calTarget) return;
  const target = calTarget;
  try {
    if (target === 'leader') await invoke('leader_calibrate', { action: 'cancel' });
    else await followerAction('cancel', 'cancelled');
    logLine('校准', '已取消并恢复旧校准');
  } catch (e) {
    logLine('校准', '取消失败: ' + e);
  }
  closeCalibration();
  refresh();
};

// ---------------------------------------------------------------------------
// Motion actions. The board is the only recorder, the only player and the only
// catalogue; this module holds no trajectory and caches no action list beyond
// what the last refresh returned. Every button stays clickable — the handler
// says why it refused (see the leader-arm follow bug above).
// ---------------------------------------------------------------------------
const ma = {
  admin: false,         // a MUTATING admin round-trip is in flight
  playInflight: false,  // a blocking play/preview call is out (seconds, not ms)
  statusInflight: false,
  recording: null,      // {scope, duration_ms} straight from the board
  draft: null,          // {draft_id, scope, duration_ms, frames}
  playing: null,
  hostStartedAt: null,  // base_host epoch; a change means drafts evaporated
  stale: true,          // list on screen is not known to be current
  lastTrash: null,      // trash name of the last delete, for one-click undo
  poll: 0,              // setInterval handle while the board is busy
  resultAt: undefined,  // finished_at of the last result already logged
};

// `play`/`preview` block for the whole action. `stop`/`status`/`list` must be
// callable EXACTLY THEN — that is the point of them — so they share no mutex
// with anything. ssh multiplexes over one ControlMaster connection, so a stop
// really does travel while a play is still hanging.
const MA_LONG = new Set(['play', 'preview']);
const MA_FREE = new Set(['stop', 'status', 'list']);

const SCOPE_CN = { arm: '机械臂', base: '底盘', full: '全身' };
const MA_ERR_CN = {
  busy: '板端拒绝：所需的底盘/机械臂已被人工或 ROS 占用',
  recording: '板端拒绝：正在录制',
  playing: '板端拒绝：有动作正在播放',
  already_recording: '板端拒绝：已经在录制',
  draft_exists: '板端拒绝：已有草稿，请先保存或丢弃',
  no_draft: '板端拒绝：没有草稿',
  not_recording: '板端拒绝：当前没有在录制',
  safety_off: '板端拒绝：总电机输出已切断',
  calibrating: '板端拒绝：机械臂校准进行中',
  arm_unavailable: '板端拒绝：从臂未上线（arm unavailable）',
  action_in_use: '板端拒绝：该动作正在播放，不能覆盖或删除',
  action_exists: '板端拒绝：动作 ID 已存在（勾选覆盖才会替换）',
  unknown_action: '板端拒绝：动作不存在',
  host_unreachable: '板端拒绝：base_host 未运行，控制 socket 连不上',
};

function maErr(obj) {
  const head = MA_ERR_CN[obj.error] || ('板端拒绝：' + obj.error);
  return obj.detail ? `${head}（${obj.detail}）` : head;
}

function maState(text, cls) {
  const el = $('mastate');
  el.textContent = text;
  el.className = 'pill ' + (cls || '');
}

function maRefreshButtons() {
  const hasDraft = !!ma.draft;
  $('marec').textContent = ma.recording ? '停止录制' : '开始录制';
  $('marec').classList.toggle('live', !!ma.recording);
  $('maprev').setAttribute('aria-disabled', String(!hasDraft));
  $('masave').setAttribute('aria-disabled', String(!hasDraft));
  $('madisc').setAttribute('aria-disabled', String(!hasDraft));
  $('maundo').setAttribute('aria-disabled', String(!ma.lastTrash));
  $('mahint').textContent = ma.stale ? '列表可能过期' : '列表已同步';
  $('mahint').className = 'pill ' + (ma.stale ? 'warn' : 'ok');
  if (ma.recording) {
    const d = ma.recording.duration_ms;
    maState(`录制中 · ${SCOPE_CN[ma.recording.scope] || ma.recording.scope}`
      + (d ? ` · ${(d / 1000).toFixed(1)}s` : ''), 'warn');
  } else if (ma.playing) {
    maState(`播放中 ${ma.playing.name} · ${Math.round(ma.playing.progress * 100)}%`, 'ok');
  } else if (ma.playInflight) maState('播放请求已发出…', 'ok');
  else if (hasDraft) {
    const f = Object.entries(ma.draft.frames).map(([k, v]) => `${k} ${v}`).join(' · ');
    maState(`草稿 ${(ma.draft.duration_ms / 1000).toFixed(1)}s · ${f}`, 'warn');
  } else maState('空闲', '');
}

async function maCall(op, extra) {
  if (!invoke) throw new Error('浏览器模式没有板端通道');
  const ip = $('ip').value.trim();
  if (!ip) throw new Error('Orin IP 为空');
  const long = MA_LONG.has(op);
  if (long && ma.playInflight) throw new Error('已有动作在播放，先点「停止播放」');
  if (!long && !MA_FREE.has(op) && ma.admin) throw new Error('上一条板端命令还没返回');
  if (long) ma.playInflight = true;
  else if (!MA_FREE.has(op)) ma.admin = true;
  try {
    const raw = await invoke('motion_action', { ip, op, ...(extra || {}) });
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error('板端返回不是 JSON: ' + raw.slice(0, 160));
    }
  } finally {
    if (long) ma.playInflight = false;
    else if (!MA_FREE.has(op)) ma.admin = false;
  }
}

/** Poll the board while it is recording or playing: the pill must track the
 *  board, not the last button press. Stops itself once the board is idle, so
 *  an idle GUI opens no ssh connections at all. */
function maPollOn() {
  if (ma.poll) return;
  ma.poll = setInterval(maStatusTick, 700);
}

function maPollOff() {
  if (!ma.poll) return;
  clearInterval(ma.poll);
  ma.poll = 0;
}

async function maStatusTick() {
  if (ma.statusInflight) return;          // slow ssh: skip, don't pile up
  ma.statusInflight = true;
  try {
    const st = await maCall('status');
    if (st.ok) {
      maApplyStatus(st);
      maRefreshButtons();
    }
  } catch {
    // A transient ssh hiccup is not news; the next tick decides.
  } finally {
    ma.statusInflight = false;
  }
  if (!ma.playing && !ma.recording && !ma.playInflight) maPollOff();
}

// The board is the only truth about drafts. If base_host restarted, the draft
// we were holding is gone — say so instead of leaving the UI waiting on a
// draft_id that no longer exists anywhere.
function maApplyStatus(st) {
  if (ma.hostStartedAt !== null && st.host_started_at !== ma.hostStartedAt
      && (ma.draft || ma.recording)) {
    logLine('动作', 'base_host 已重启，板端内存里的录制草稿已丢失');
  }
  ma.hostStartedAt = st.host_started_at;
  ma.recording = st.recording || null;
  ma.draft = st.draft || null;
  ma.playing = st.playing || null;
  const r = st.last_result;
  // Keyed on finished_at, not on the result string: two preemptions in a row
  // are two events and both deserve a line.
  if (r && r.result && r.finished_at !== ma.resultAt) {
    const first = ma.resultAt === undefined;   // adopt the pre-launch result quietly
    ma.resultAt = r.finished_at;
    if (!first && r.result !== 'succeeded') {
      logLine('动作', `动作结束：${r.result}${r.by ? ' by ' + r.by : ''}`
        + (r.action_id ? `（${r.action_id}）` : ''));
    }
  }
  if (ma.playing || ma.recording) maPollOn();
}

function maRow(a) {
  const tr = document.createElement('tr');
  const text = [a.label, a.description].filter(Boolean).join(' · ') || '—';
  tr.innerHTML =
    `<td class="maid"></td><td></td><td></td><td class="mad"></td><td class="maact"></td>`;
  tr.children[0].textContent = a.action_id;
  tr.children[1].textContent = SCOPE_CN[a.scope] || a.scope;
  tr.children[2].textContent = text;
  tr.children[3].textContent = (a.duration_ms / 1000).toFixed(1) + 's';
  const play = document.createElement('button');
  play.textContent = '试播';
  play.onclick = () => maPlay(a.action_id);
  const del = document.createElement('button');
  del.textContent = '删除';
  del.onclick = () => maDelete(a.action_id);
  tr.children[4].append(play, del);
  return tr;
}

function maInvalidRow(bad) {
  const tr = document.createElement('tr');
  tr.className = 'bad';
  tr.innerHTML = '<td class="maid"></td><td colspan="4"></td>';
  tr.children[0].textContent = bad.action_id;
  tr.children[1].textContent = `文件损坏（${bad.error}）：${bad.detail || ''}`;
  return tr;
}

function maPaint(listing) {
  const body = $('malistbody');
  body.textContent = '';
  for (const a of listing.actions) body.appendChild(maRow(a));
  for (const bad of listing.invalid || []) body.appendChild(maInvalidRow(bad));
  if (!listing.actions.length && !(listing.invalid || []).length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5"></td>';
    tr.children[0].textContent = '板端还没有动作，录一个试试';
    body.appendChild(tr);
  }
}

async function maRefresh(quiet) {
  try {
    const listing = await maCall('list');
    if (!listing.ok) { logLine('动作', maErr(listing)); return; }
    maPaint(listing);
    ma.lastTrash = (listing.trash || [])[0] || null;
    const st = await maCall('status');
    if (st.ok) maApplyStatus(st);
    ma.stale = false;
  } catch (e) {
    // Keep whatever is on screen but never pass it off as live state.
    ma.stale = true;
    if (!quiet) logLine('动作', '刷新失败：' + e);
  }
  maRefreshButtons();
}

$('marefresh').onclick = () => maRefresh(false);

$('marec').onclick = async () => {
  const scope = $('mascope').value;
  try {
    if (ma.recording) {
      const r = await maCall('record-stop');
      if (!r.ok) { logLine('动作', maErr(r)); await maRefresh(true); return; }
      ma.recording = null;
      ma.draft = r.draft;
      logLine('动作', `录制结束：${(r.draft.duration_ms / 1000).toFixed(1)}s`
        + (r.draft.capped ? '（已到长度上限）' : ''));
    } else {
      if (calTarget) { logLine('动作', '无法录制：校准进行中'); return; }
      if (scope !== 'base' && !connected) {
        logLine('动作', '提示：主臂未连接，仍可用板载手柄示教机械臂');
      }
      const r = await maCall('record-start', { scope });
      if (!r.ok) { logLine('动作', maErr(r)); return; }
      ma.recording = { scope, duration_ms: 0 };
      ma.draft = null;
      maPollOn();
      logLine('动作', `开始录制（${SCOPE_CN[scope]}）：键盘 / 手柄 / 主臂都会被采集`);
    }
  } catch (e) {
    logLine('动作', '录制命令失败：' + e);
  }
  maRefreshButtons();
};

$('maprev').onclick = async () => {
  if (!ma.draft) { logLine('动作', '无法预览：没有草稿（先录一段）'); return; }
  try {
    logLine('动作', '预览草稿（用的就是 MCP 那个板端播放器）');
    maPollOn();                       // the pill tracks the board, not this call
    const r = await maCall('preview');
    logLine('动作', r.ok ? '预览完成' : maErr(r));
  } catch (e) {
    logLine('动作', '预览失败：' + e);
  }
  await maRefresh(true);
};

async function maPlay(id) {
  try {
    logLine('动作', '试播 ' + id);
    maPollOn();
    const r = await maCall('play', { id });
    logLine('动作', r.ok ? `试播完成 ${id}`
      : `${id}：${r.result || ''}${r.by ? ' by ' + r.by : ''} ${maErr(r)}`);
  } catch (e) {
    logLine('动作', '试播失败：' + e);
  }
  await maRefresh(true);
}

$('mastop').onclick = async () => {
  try {
    const r = await maCall('stop');
    logLine('动作', r.ok ? '已发送停止' : maErr(r));
  } catch (e) {
    logLine('动作', '停止失败：' + e);
  }
};

$('madisc').onclick = async () => {
  if (!ma.draft) { logLine('动作', '没有草稿可丢弃'); return; }
  try {
    await maCall('record-discard');
    ma.draft = null;
    logLine('动作', '草稿已丢弃');
  } catch (e) {
    logLine('动作', '丢弃失败：' + e);
  }
  maRefreshButtons();
};

$('masave').onclick = async () => {
  const id = $('maid').value.trim();
  if (!ma.draft) { logLine('动作', '无法保存：没有草稿'); return; }
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(id)) {
    logLine('动作', '无法保存：动作 ID 必须是 [a-z][a-z0-9_-]，最长 32');
    return;
  }
  try {
    const r = await maCall('save', {
      id, label: $('malabel').value.trim(), desc: $('madesc').value.trim(),
      overwrite: $('maover').checked,
    });
    if (!r.ok && r.error === 'action_exists') {
      // Explicit second confirmation, in the page itself: no native dialog to
      // be swallowed, and no silent overwrite of somebody else's recording.
      logLine('动作', `板端已有「${id}」。要替换请勾选「允许覆盖同名」再点保存；草稿保留`);
      return;
    }
    if (!r.ok) { logLine('动作', maErr(r) + '（草稿保留）'); return; }
    ma.draft = null;
    logLine('动作', `已保存 ${r.action_id}（${SCOPE_CN[r.scope]} ${(r.duration_ms / 1000).toFixed(1)}s）`
      + (r.replaced ? '，覆盖了旧版本' : ''));
  } catch (e) {
    logLine('动作', '保存失败（草稿保留）：' + e);
  }
  await maRefresh(true);
};

// Confirmation without a dialog: the ID typed in the box must match the row.
// Same trick as the overwrite checkbox — the confirmation is a visible piece of
// UI state, so it can never be swallowed the way a native dialog or a
// `disabled` button can.
async function maDelete(id) {
  if ($('maid').value.trim() !== id) {
    $('maid').focus();
    logLine('动作', `确认删除「${id}」：先把 ${id} 填进动作 ID 框，再点这一行的删除`);
    return;
  }
  try {
    const r = await maCall('delete', { id });
    if (!r.ok) { logLine('动作', maErr(r)); return; }
    ma.lastTrash = r.trash_name;
    logLine('动作', `已删除 ${id} → .trash/${r.trash_name}，可撤销`);
  } catch (e) {
    logLine('动作', '删除失败：' + e);
  }
  await maRefresh(true);
}

$('maundo').onclick = async () => {
  if (!ma.lastTrash) { logLine('动作', '没有可撤销的删除'); return; }
  try {
    const r = await maCall('restore', { trash: ma.lastTrash });
    logLine('动作', r.ok ? `已恢复 ${r.action_id}` : maErr(r));
  } catch (e) {
    logLine('动作', '撤销失败：' + e);
  }
  await maRefresh(true);
};

/** Entering the teleop tab: the board's catalogue is the only truth, re-read it. */
export function onEnterLeader() {
  maRefresh(true);
}

/** Leaving the tab: never let a recording or a GUI-started action outlive the
 *  panel that can stop it. Board-side leases and the watchdog are the backstop,
 *  this is the polite half. */
export function onLeaveLeader() {
  maPollOff();
  if (!invoke) return;
  // `playInflight` too: a GUI-started action the board has not reported yet is
  // exactly the one that must not outlive this panel.
  if (ma.playing || ma.playInflight) maCall('stop').catch(() => {});
  if (ma.recording) {
    maCall('record-stop')
      .then(r => {
        ma.recording = null;
        ma.draft = r && r.ok ? r.draft : null;
        logLine('动作', '离开遥控页：录制已停止' + (ma.draft ? '，草稿保留' : ''));
        maRefreshButtons();
      })
      .catch(e => logLine('动作', '离开遥控页时停止录制失败：' + e));
  }
}

maRefreshButtons();

const ev = window.__TAURI__ && window.__TAURI__.event;
if (ev) {
  ev.listen('leader', ({ payload: p }) => {
    connected = p.connected;
    following = p.following;
    aligned = p.aligned;
    if (calTarget !== 'follower') {
      for (let i = 0; i < 6; i++) {
        $('lj' + i).textContent = p.joints[i] !== undefined ? p.joints[i] : '—';
      }
    }
    refresh(false);
  });
}

refresh();

// Auto-connect at launch; quiet failure if no leader arm is plugged in.
if (invoke) connectLeader(true);
// The teleop tab is the landing page, so pull the board's action list once at
// launch. Quiet: an unreachable board is normal before the robot is powered.
if (invoke) maRefresh(true);
