// Shared helpers + the one cross-module mutable: which tab is showing.
export const invoke = window.__TAURI__ && window.__TAURI__.core
                    ? window.__TAURI__.core.invoke : null;
export const $ = id => document.getElementById(id);
export const S = { page: 'zmq' };

// 连接参数持久化 —— 统一记在一处:localStorage "lekiwi.conn" 一个 JSON。
// 覆盖 ZMQ/SSH 共用的 ip、port、视觉 vip、语音 voip、主臂 lport。
// 必须在本模块 import 时就恢复:zmq.js 底部的启动自动连接读的是恢复后的值。
const CONN_KEY = 'lekiwi.conn';
const CONN_FIELDS = ['ip', 'port', 'vip', 'voip', 'lport'];
try {
  const saved = JSON.parse(localStorage.getItem(CONN_KEY) || '{}');
  for (const f of CONN_FIELDS) {
    const el = $(f);
    if (el && saved[f]) el.value = saved[f];   // 空值不覆盖(lport 留空=自动扫描)
  }
} catch { /* 坏数据视同无 */ }

export function saveConn() {
  const o = {};
  for (const f of CONN_FIELDS) { const el = $(f); if (el) o[f] = el.value.trim(); }
  try { localStorage.setItem(CONN_KEY, JSON.stringify(o)); } catch { /* 满/禁用则罢 */ }
}
for (const f of CONN_FIELDS) {
  const el = $(f);
  if (el) el.addEventListener('change', saveConn);
}
