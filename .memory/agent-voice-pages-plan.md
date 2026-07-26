---
name: agent-voice-pages-plan
description: GUI 语音改版计划已定稿（2026-07-21，codex+kimi 双评审）— Agent/Voice 分页、三轴切换收口 voice-daemon、板端统一 config；动语音/GUI 前必读 docs/agent-voice-pages-plan.html
metadata:
  type: project
---

2026-07-21 定稿 GUI 语音改版计划，全文 `docs/agent-voice-pages-plan.html`（v2 已吸收
codex gpt-5.6-sol 25 条 + kimi 24 条评审，采纳/驳回见其 §10）。

核心决策（改语音/GUI 相关代码前先读原文）：
- **Tab 顺序 Vision → 🎤 Voice → 🦞 Agent**。现 voice 页改名 Agent 页（只改路由与文案，
  DOM id/内部函数名不动）；新建 Voice 调试页（设备状态条 + ASR 转写台 + TTS 试听台 +
  Vision 播报开关）。
- **config 全局只有两个**：Mac 现有 lekiwi-console/config.json（连接+UI）；板端新增统一
  `~/.config/lekiwi/config.json`（desired state，voice-daemon 唯一写入口，其它板端服务
  只读）。入 git 的是 `board/config.example.json`——整个板端统一 config 样例（含 LLM
  选择/presets/搭配/开关，非 voice 专属），setup.sh 缺失才拷贝——不放 board/home 镜像树
  （会被 rsync 部署踩掉运行时写入）。Hermes config.yaml 由统一 config **下发**（补丁式），/health 回报
  desired/applied/drift。
- **pair 是引擎选择的唯一持久状态**：preset（flash/mimo/…）自带 ASR+TTS 搭配，运行值 =
  当前 preset.pair 的投影；Voice 调试页改引擎 = ephemeral 覆盖不落盘。消除双状态漂移。
- **长操作一律 202+job + feed 进度**（Rust 代理 15s 超时硬约束）；切大脑落盘前必须过
  真实 1-token completion 探针（端口健康≠模型可用）+ ensure_session；yaml 补丁只允许
  model.* 两键 + 幂等 upsert providers 段，其余结构 diff 必须为空（安全链不动）。
- **P0a 前置实验定架构**：CPython+onnxruntime 卸载大概率不还内存（板剩 195MB）→
  可能采用引擎子进程宿主（kill 保证归还）。内存数字一律实测口径（PSS+swap，非 RSS）。
- 切换全局锁串行 + drain 线程池（防推理中 unload segfault）；/say 补 latest-wins；
  Vision 播报桥在**板端**（vlm→voice daemon，GUI 桥没有事件源——vision 轮询绑 Tab 可见性）。
- 砍：piper、在线 ASR、自动降级切大脑、多 GUI owner 仲裁。推迟：zipformer 流式（P3，
  须一等公民流式接口）、Omni 大脑（P4，Mac omnivla 包 omni-server，实施前单独细化）。
- 阶段 P0a/P0b/P1/P2/P3/P4 各有验收门，P1 只验现有引擎（sensevoice/edge/melo）。

**评审工具用法坑**：kimi CLI 的 `-p` 与 `-y`/`--auto` 互斥，非交互评审只能裸 `kimi -p`
（读操作免审批）；codex 用 `codex exec -m gpt-5.6-sol --dangerously-bypass-approvals-and-sandbox`。

## 转写台不是一个状态（2026-07-26 重构）

原设计里「转写台」是状态机的一个态（`DEBUG`），**与对话互斥**：开台先 `do_stop()`
停对话。那个互斥没有技术必要 —— 采集一路、VAD 一路、ASR 一段解一次，谁要看谁读环 ——
但它派生出一长串 bug，每一个都是在给这个模式打补丁：接管事件没人渲染、Agent 页把
`debug` 当 `idle` 显示成「待机」、「结束对话」按钮在没有对话时照样出现、解码期间状态
一变整段被静默丢弃、试听 TTS 一次就把台子关了。

现在：**麦克风按引用计数**。`state` 只描述对话（idle/listening/thinking/speaking），
`bench_on` 是正交的布尔，`/health` 和 `/state` 各带一个 `bench` 字段。
`_mic_wanted()` = `bench_on or state in (LISTENING, THINKING, SPEAKING)`，
`_sync_mic()` 是**唯一**知道麦克风该不该开的地方（以前三处 switch 的 finally
里各抄了一遍 `if DEBUG/else`）。段的记录也塌成一条：`_record_seg` 无条件进环，
不再有 `to_debug` 分支；`asr_seg` 事件删了 —— 它**从来没有被渲染过**，是条死机制。

**How to apply:** 判断「要不要为 X 加一个状态」时先问：X 和现有状态是正交的吗？
正交就该是一个独立的布尔 + 一个 `_sync_*` 汇聚点，塞进状态机会让每个使用者都要
复制一遍互斥判断。真正需要独占的只有**改变机器人听到什么**的动作（临时换 ASR
引擎、扫 VAD 参数、流式模式、段回放），那些仍然只在台子开着时可用、关台还原。

相关：[[voice-frontend-s2]]、[[hermes-voice-agent-plan]]、[[lekiwi-gui-tauri]]、
[[board-memory-ceiling]]、[[vlm-stack-orin]]。
