// Shared helpers + the one cross-module mutable: which tab is showing.
export const invoke = window.__TAURI__ && window.__TAURI__.core
                    ? window.__TAURI__.core.invoke : null;
export const $ = id => document.getElementById(id);
export const S = { page: 'zmq' };

// 连接参数 —— 唯一真相源是 ~/.config/lekiwi-console/config.json(通常符号链接到
// 仓库 gui/config.local.json,可手改可 grep)。启动时 load_config 灌进输入框,
// GUI 里改了字段就 save_config 写回同一个文件——没有 localStorage 第二真相,
// 手改文件和 GUI 改动永不打架。样例见 gui/config.example.json;release 下 ui/
// 资产打进二进制,配置必须在文件系统里所以走 Rust 读写。
// 顶层 await:依赖本模块的 zmq.js 启动自动连接必须等灌值完成后再读。
// 浏览器模式(无 invoke)不持久化,只用 index.html 内置默认。
const CONN_FIELDS = ['ip', 'port', 'vip', 'voip', 'lport', 'rosip', 'rosport',
                     'scanTopic', 'depthTopic', 'frontTopic', 'wristTopic'];
localStorage.removeItem('lekiwi.conn');   // 清掉旧两套方案的遗留残值
let cfg = {};
try {
  cfg = invoke ? JSON.parse(await invoke('load_config')) : {};
} catch { /* 无 config.json / 坏 JSON 视同无 */ }
for (const f of CONN_FIELDS) {
  const el = $(f);
  if (el && cfg[f]) el.value = cfg[f];   // 空值不覆盖(lport 留空=自动扫描)
}

export function saveConn() {
  if (!invoke) return;
  // 读-改-写:只更新字段键,保留 "//" 注释等其它键
  for (const f of CONN_FIELDS) { const el = $(f); if (el) cfg[f] = el.value.trim(); }
  invoke('save_config', { text: JSON.stringify(cfg, null, 2) + '\n' }).catch(() => {});
}
for (const f of CONN_FIELDS) {
  const el = $(f);
  if (el) el.addEventListener('change', saveConn);
}
