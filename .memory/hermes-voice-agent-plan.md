---
name: hermes-voice-agent-plan
description: LeKiwi 语音大脑方案 — Hermes Agent + DeepSeek-V4-Flash 上板，自研 voice-frontend、受限 drive MCP、端侧 Qwen3-VL；方案在 docs，经 codex 两轮评审
metadata:
  type: project
---

2026-07-19 定稿方案 `docs/hermes-lekiwi-voice-agent-plan.html`（codex gpt-5.6-sol 两轮评审：
一轮 17 条推倒重写，二轮补 4 个安全闭环）。要点与选型定谳：

- **大脑**：Hermes Agent（Nous Research「爱马仕龙虾」）装 Orin 板上（uv 自带 py3.11，
  不动系统 3.10），云端模型 **deepseek-v4-flash**（`deepseek-chat` 别名 2026-07-24 弃用；
  价 $0.14/M 未命中 · $0.0028/M 缓存命中 · $0.28/M 输出，2026-07-19 官网价）。
  **机器人用独立 profile**（`hermes profile create robot` + 显式裁剪 toolset 关 terminal），
  已装 **v0.18.2 (2026.7.7.2) commit e598cef8**（2026-07-19 装，git 方式，`~/.hermes/hermes-agent`；
  profile 在 `~/.hermes/profiles/robot`，wrapper 命令 `robot`）。版本必须 pin——它的 API/配置字段随版本漂移，配置以向导实际产物为准，不手写猜 YAML。
- **听说**：Hermes 的 Ctrl+B 只是 TUI 对讲，**常驻麦克风闭环要自研 voice-frontend**
  （ALSA→VAD/PTT→faster-whisper small int8 CPU→Hermes Sessions API SSE→edge-tts，
  熔断降级本地 piper zh_CN-huayan；半双工闭麦）。板子无板载音频，需 USB speakerphone。
- **安全模型（评审最重的部分）**：SKILL.md 不是安全边界。四层：profile 裁剪（够不着
  shell）→ drive MCP schema 硬钳位（语音档 ≤0.15m/s ≤2s）→ base_host v2 仲裁
  （**身份由通道决定**：特权走 Unix socket 文件权限认证，tcp:5555 归人工档且人工恒压制
  LLM；分域 watchdog；LLM 300-500ms 短租约；**ARM 武装态权威存 base_host**，原子核
  ARM∧lease）→ 物理急停。现 base_host 全局 last_cmd watchdog 有缺陷（臂消息会刷新底盘
  watchdog），升级是控车前置工程。VLM caption 定性**不可信观测**（视觉提示注入是真攻击面），
  从不直接触发运动，模糊指令须复述确认。
- **视觉**：Qwen3-VL-2B-Instruct GGUF Q4 起步（4B 需压测余量实证）@ llama.cpp CUDA
  自编（固定 commit，`CMAKE_CUDA_ARCHITECTURES=87`，启动日志核实 GPU offload）；
  三层栈 llama-server(:8091 lo) ← vlm-daemon(:8090 全端点 token，有界队列 latest-wins，
  caption 带 frame_ts) ← 只读 MCP 三工具。相机用 /dev/v4l/by-id；CSI 接入是新 source
  类型（GStreamer/NVMM），不是加行配置。
- **GUI**：gui/ Tauri 加 🦞 Tab；Hermes 官方 **API Server**（:8642，Bearer，
  Sessions API SSE 带 tool.started/completed 事件）；voice-frontend 与 GUI 用同一
  session id 共会话；所有 token 只在 Rust 后端，vlm-daemon 也走 Rust 代理。
- **8GB 内存是「待联合压测」不是结论**；nvpmodel -m 2 只加算力不加内存。
- **实施顺序 S0-S6**：安全控制面(S0)先行，S4（LLM 摸运动）依赖 S0+S2+S3 全过。

**Why:** 把「能演示的高权限 Agent」和「可安全上车的语音大脑」区分开是两轮评审的核心
教训——安全边界必须建在模型够不着的层（工具裁剪/schema/仲裁/物理急停），不是提示词。

**How to apply:** 实施时按方案 S0-S6 阶段与 gate 走；改 base_host 前重读 §2.2。
v2 时新增状态 PUB（tcp:5557，电池/臂状态/watchdog/ARM 租约，变化即推），替代
/tmp 文件+ssh 转发；但主机指标（功耗/CPU/温度）保留 ssh 拉——独立于板上守护进程的
诊断通道是刻意设计（2026-07-19 与用户定谳）。
相关：[[lekiwi-gui-tauri]]、[[lekiwi-robot-target]]、[[rdk-gs130wi-camera]]、
[[jetson-platform-baseline]]。
