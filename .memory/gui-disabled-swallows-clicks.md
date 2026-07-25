---
name: gui-disabled-swallows-clicks
description: GUI 按钮别用原生 disabled 门控——点击被吞掉、无日志无报错，故障不可证伪；还有 ZMQ PUSH 不自愈的坑
metadata:
  type: feedback
---

控制台 GUI（`gui/ui/`）里**不要用原生 `disabled` 属性门控动作按钮**，改成常开 +
`aria-disabled` 只做视觉变暗，在 onclick 里判断前置条件并 `logLine` 说明拒绝原因。

**Why:** 2026-07-22 排查「主臂跟随点了没反应」花了很久。原生 `disabled` 的按钮会把
click **整个吞掉**——不触发 handler、不报错、不进日志。于是现象变成「界面显示已对齐、
关节数字在跳、按钮看着能点，但点下去全世界没有任何反应」，**故障不可证伪**：
前端无日志、后端无调用，只能靠往 Rust 里塞 eprintln 逐层证伪（证明了
`leader_follow` 这个 #[tauri::command] 从未被调用），才把范围收敛到「点击那一刻
按钮其实是 disabled」。codex 独立复核结论一致：现有代码里不存在「进了 onclick 却
没调 invoke」的路径，所以只可能是点击压根没进 handler。

后端本来就有兜底（`leader_follow` 只在 `zero.is_some()` 时才置 following），
前端 disabled 是重复的第二道门，代价却是吞掉全部诊断信息——典型的坏交易。

**How to apply:** 加新的条件可用按钮时，一律 `aria-disabled` + handler 内判断 +
可见反馈。CSS 用 `button[aria-disabled="true"] { opacity:.4 }`，**不要**加
`pointer-events:none`（那等于又把点击吞了）。

## 同一次排查的第二个坑：ZMQ PUSH 不自愈

`gui/src-tauri/src/main.rs` 的 base 命令通道用纯 Rust `zeromq` crate 的 PushSocket，
**它不会自动重连**。board 上 base_host 一重启，PUSH 就永久哑掉，帧被 200ms 超时静默
吞掉，而 GUI 的 `connected` 仍是 true，界面显示「已连接」——底盘和跟随命令全部失效
但毫无提示。同文件的日志 SUB worker 早就有 2s 自愈探测（注释还专门写了"board reboot
不能让日志条静默死掉"），唯独命令通道没有——不对称的特殊情况。

已修：抽出 `push_or_drop()`，发送超时/失败即丢 socket，配合 2s 自愈探测重连（和日志
worker 同一套路）。诊断手法值得复用：**`lsof -nP -iTCP -a -p <gui_pid>` 看 GUI 到
板子 :5555/:5556 的连接在不在**，比读代码快得多。

相关：[[lekiwi-gui-tauri]] [[commit-discipline]]
