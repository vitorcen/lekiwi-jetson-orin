// Top status bar: Orin vitals polled over ssh (Tauri backend only). One
// `sysinfo` round-trip every 4 s returns newline "key value..." lines; we parse
// and paint the bar. Two power sources are shown:
//   ⚡ 主机  — board power draw (VDD_IN, W). The host battery (E351S) sits
//             behind a DC-DC that regulates to 19 V, so its charge is not
//             measurable on the Orin; live power is the useful host metric.
//   🔋 舵机  — servo pack voltage (WitMotion 11.1 V 3S) via base_host, turned
//             into a rough %. Offline when base_host isn't publishing.
import { $, invoke } from './state.js';

// Paint one mini gauge: width = pct, colour ramps blue→amber→red past warnHi.
function bar(fillId, pct, warnHi = 85, midHi = 65) {
  const p = Math.max(0, Math.min(100, pct));
  const el = $(fillId);
  el.style.width = p + '%';
  el.style.background = pct > warnHi ? '#f38ba8' : pct > midHi ? '#f9e2af' : '#7ea6e0';
}

function offline() {
  $('sbDot').classList.remove('up');
  for (const id of ['pwrTxt', 'battTxt', 'armTxt', 'cpuTxt', 'gpuTxt', 'ramTxt', 'hdTxt', 'tempTxt']) {
    $(id).textContent = '--';
    $(id).style.color = '';      // clear stale warn colours (temp/power red)
  }
  for (const id of ['battFill', 'cpuFill', 'gpuFill', 'ramFill', 'hdFill'])
    $(id).style.width = '0';
}

// Servo pack: 3S Li-Po, 12.6 V full .. 9.9 V empty (3.3 V/cell). Rough — sags
// under motor load, same caveat as any voltage-only fuel gauge.
function battPct(v) {
  return Math.round(Math.max(0, Math.min(100, (v - 9.9) / (12.6 - 9.9) * 100)));
}

function paint(kv) {
  // pwr: mV mA -> W
  if (kv.pwr && kv.pwr.length === 2) {
    const w = (+kv.pwr[0]) * (+kv.pwr[1]) / 1e6;
    $('pwrTxt').textContent = w.toFixed(1) + 'W';
    $('pwrTxt').style.color = w > 20 ? '#f9e2af' : '#cdd6f4';
  } else $('pwrTxt').textContent = '--';

  // sbatt: servo pack volts (empty string when base_host is down)
  const v = parseFloat(kv.sbatt && kv.sbatt[0]);
  if (v > 0) {
    const pct = battPct(v);
    $('battTxt').textContent = `${pct}% ${v.toFixed(1)}V`;
    // Battery colours by charge (low = red), the opposite ramp from load gauges.
    $('battFill').style.width = pct + '%';
    $('battFill').style.background = pct > 45 ? '#7ee2a8' : pct > 20 ? '#f9e2af' : '#f38ba8';
    $('battTxt').style.color = pct > 20 ? '#cdd6f4' : '#f38ba8';
  } else {
    $('battTxt').textContent = '离线';
    $('battTxt').style.color = '#6b7394';
    $('battFill').style.width = '0';
  }

  // sarm: arm torque state from base_host ("limp"|"holding"|"none")
  const arm = kv.sarm && kv.sarm[0];
  if (arm === 'limp') {
    $('armTxt').textContent = '松弛';
    $('armTxt').style.color = '#7ee2a8';
  } else if (arm === 'holding') {
    $('armTxt').textContent = '锁定';
    $('armTxt').style.color = '#f9e2af';
  } else if (arm === 'none') {
    $('armTxt').textContent = '未接';
    $('armTxt').style.color = '#6b7394';
  } else {
    $('armTxt').textContent = '离线';
    $('armTxt').style.color = '#6b7394';
  }

  // cpu: loadavg1 / nproc
  if (kv.cpu && kv.cpu.length === 2) {
    const pct = Math.round(+kv.cpu[0] / (+kv.cpu[1] || 6) * 100);
    $('cpuTxt').textContent = Math.min(100, pct) + '%';
    bar('cpuFill', pct);
  }

  // gpu: per-mille (or -1)
  const gp = kv.gpu ? +kv.gpu[0] : -1;
  if (gp >= 0) { $('gpuTxt').textContent = Math.round(gp / 10) + '%'; bar('gpuFill', gp / 10); }
  else { $('gpuTxt').textContent = 'n/a'; $('gpuFill').style.width = '0'; }

  // mem: MemTotal MemAvailable (kB) — unified memory
  if (kv.mem && kv.mem.length === 2) {
    const tot = +kv.mem[0], used = tot - +kv.mem[1];
    $('ramTxt').textContent = `${(used / 1048576).toFixed(1)}/${(tot / 1048576).toFixed(1)}G`;
    bar('ramFill', used / tot * 100);
  }

  // disk: used total (MB)
  if (kv.disk && kv.disk.length === 2) {
    const used = +kv.disk[0], tot = +kv.disk[1];
    $('hdTxt').textContent = `${(used / 1024).toFixed(0)}/${(tot / 1024).toFixed(0)}G`;
    bar('hdFill', used / tot * 100);
  }

  // temp: max thermal zone, milli-°C
  const t = parseInt(kv.temp && kv.temp[0]) / 1000;
  if (t > 0) {
    $('tempTxt').textContent = t.toFixed(0) + '°C';
    $('tempTxt').style.color = t > 80 ? '#f38ba8' : t > 65 ? '#f9e2af' : '#cdd6f4';
  }

  $('sbDot').classList.add('up');
}

async function poll() {
  if (document.hidden) return;      // minimized window must not ssh all night
  const ip = ($('ip') && $('ip').value.trim()) || '';
  if (!ip) { offline(); return; }   // no board configured yet
  try {
    const txt = await invoke('sysinfo', { ip });
    const kv = {};
    for (const line of txt.trim().split('\n')) {
      const p = line.trim().split(/\s+/);
      if (p[0]) kv[p[0]] = p.slice(1);
    }
    paint(kv);
  } catch {
    offline();          // ssh failed / board down: red dot, dashes
  }
}

if (invoke) { poll(); setInterval(poll, 4000); }
else offline();         // browser mode: no ssh backend
