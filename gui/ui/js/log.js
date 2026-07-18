// Bottom log panel: one generic sink for every teleop event, local or remote.
//
// Remote lines arrive as Tauri "log" events forwarded by the Rust log-bus SUB
// (board PUB on :5556 — the gamepad daemon echoes each button/axis here, which
// is what lets you press the pad and read off which control it maps to). Local
// modules (keyboard drive, leader arm) call logLine() directly. The wire format
// is a JSON {src, text} line, but a bare string is accepted too so any future
// board process can publish here without ceremony.
import { $ } from './state.js';

const MAX = 400;   // keep the DOM bounded; oldest rows drop off the top

function pad2(n) { return String(n).padStart(2, '0'); }

export function logLine(src, text) {
  const b = $('logbody');
  if (!b) return;
  const stick = b.scrollTop + b.clientHeight >= b.scrollHeight - 4;  // at bottom?
  const t = new Date();
  const row = document.createElement('div');
  row.className = 'logrow src-' + src;
  const ts = document.createElement('span');
  ts.className = 'lt';
  ts.textContent = `${pad2(t.getHours())}:${pad2(t.getMinutes())}:${pad2(t.getSeconds())}`;
  const tag = document.createElement('span');
  tag.className = 'lsrc';
  tag.textContent = src;
  const msg = document.createElement('span');
  msg.className = 'lmsg';
  msg.textContent = text;                 // textContent: board text is untrusted
  row.append(ts, tag, msg);
  b.appendChild(row);
  while (b.childElementCount > MAX) b.removeChild(b.firstChild);
  if (stick) b.scrollTop = b.scrollHeight;   // follow the tail unless scrolled up
}

// Remote log bus → panel.
const ev = window.__TAURI__ && window.__TAURI__.event;
if (ev) {
  ev.listen('log', ({ payload }) => {
    let src = '板', text = payload;
    try {
      const o = JSON.parse(payload);
      if (o && typeof o === 'object') { src = o.src || src; text = o.text ?? payload; }
    } catch { /* plain string: keep as-is */ }
    logLine(src, text);
  });
}

const clr = $('logclear');
if (clr) clr.onclick = () => { const b = $('logbody'); if (b) b.textContent = ''; };
