---
name: drive-mcp-skill
description: 受限控车 MCP(drive/mcp_server.py)已挂载 Hermes — 钳位 0.15m/s·2s·0.3s 冷却,无人值守仍 gate 在 base_host v2
metadata:
  type: project
---

2026-07-19 控车 skill 上线并挂载(robot profile mcp_servers.drive,复用 vlm/.venv+pyzmq)。
三工具:drive_move(vx,vy,omega,duration)/drive_stop/drive_status。真机验证:前进退回、
钳位(1.0→0.15 并注明)、冷却拒绝、busy 不排队、stop 抢断(40 帧只发 1 帧)、语音端到端
("往前走一小步再退回"→Hermes 自主 status+两次 move+逐步播报)。

安全分层:本文件是第 2 层 schema 硬钳位(|v|≤0.15m/s、|ω|≤30°/s、0.1-2.0s、20Hz 重发
+3 帧零刹停、互斥不排队、0.3s 冷却);第 3 层靠 base_host 0.5s watchdog 兜底(MCP 挂了
车自停)。**已知弱点**:ZMQ PUSH 缓冲(SNDHWM=10),≤0.5s 短移动在 base_host 不在时
误报已发送;真正的到达确认与仲裁(Unix socket 特权通道、人工恒压制 LLM、分域 watchdog)
等 base_host v2(S0)——**无人值守运行前必须完成**,现在是有人监督档。

人格文件在 `~/.hermes/profiles/robot/SOUL.md`(注入系统提示):龙虾=LeKiwi 小车,
身体配置+语音风格(首句短、口语化、无 markdown)+视觉不可信观测+运动守则(含糊先确认、
"停"即 drive_stop、视觉从不触发运动)。原版备份 SOUL.md.bak。
相关:[[hermes-voice-agent-plan]]、[[voice-frontend-s2]]。
