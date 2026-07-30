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
function refresh() {
  $('lconn').textContent = connected ? '断开主臂' : '连接主臂';
  $('lalign').setAttribute('aria-disabled', String(!connected));
  $('lfollow').setAttribute('aria-disabled', String(!connected || !aligned));
  $('lcal').setAttribute('aria-disabled', String(!connected));
  $('lfollow').textContent = following ? '停止跟随' : '开始跟随';
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
  if (calTarget) { logLine('主臂', '校准进行中，不能开始跟随'); return; }
  if (!connected) { logLine('主臂', '无法跟随：主臂未连接'); return; }
  if (!aligned) { logLine('主臂', '无法跟随：未对齐零位（先摆中位再点「对齐零位」）'); return; }
  const on = !following;
  try {
    await invoke('leader_follow', { on });
  } catch (e) {
    setState('跟随命令失败: ' + e, 'bad');
    logLine('主臂', '跟随命令失败: ' + e);
    return;                            // state stays as the backend last reported
  }
  following = on;
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
    refresh();
  });
}

refresh();

// Auto-connect at launch; quiet failure if no leader arm is plugged in.
if (invoke) connectLeader(true);
