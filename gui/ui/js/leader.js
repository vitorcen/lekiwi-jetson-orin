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

function setState(text, cls) {
  const el = $('lstate');
  el.textContent = text;
  el.className = 'pill ' + cls;
}

function refresh() {
  $('lconn').textContent = connected ? '断开主臂' : '连接主臂';
  $('lalign').disabled = !connected;
  $('lfollow').disabled = !connected || !aligned;
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
    refresh();
    return;
  }
  await connectLeader(false);
};

// Glide the follower to its calibrated middle pose (needs ZMQ connected).
$('lmid').onclick = () => {
  if (invoke) invoke('zmq_arm_mid').catch(() => {});
  logLine('主臂', '从臂摆中位');
};

$('lalign').onclick = async () => {
  if (!invoke) return;
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
  following = !following;
  await invoke('leader_follow', { on: following }).catch(() => {});
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

const ev = window.__TAURI__ && window.__TAURI__.event;
if (ev) {
  ev.listen('leader', ({ payload: p }) => {
    connected = p.connected;
    following = p.following;
    aligned = p.aligned;
    for (let i = 0; i < 6; i++) {
      $('lj' + i).textContent = p.joints[i] !== undefined ? p.joints[i] : '—';
    }
    refresh();
  });
}

refresh();

// Auto-connect at launch; quiet failure if no leader arm is plugged in.
if (invoke) connectLeader(true);
