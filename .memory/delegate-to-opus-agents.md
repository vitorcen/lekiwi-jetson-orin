---
name: delegate-to-opus-agents
description: 用户要求：功能开发按需派 Opus 子代理干活，节省主会话（Fable）上下文
metadata:
  type: feedback
---

2026-07-19 用户明确说：可以按需调用 Opus Agent 开发一些功能，节省 Fable 主会话的上下文。

**Why:** 主会话上下文是稀缺资源，长任务（Hermes 语音方案 S0-S6 这类多阶段工程）容易撑爆；
把边界清晰、可独立验收的功能模块交给子代理（model: opus）实现，主会话只保留结论与集成。

**How to apply:** 遇到"写一个独立模块/脚本/前端页面"这类边界清晰的开发任务，用 Agent 工具
（model 指定 opus）下发，prompt 里带足上下文（文件路径、约束、验收标准）；主会话负责审查
产出与集成。琐碎小改动或需要全局上下文的架构决策仍由主会话直接做。
相关：[[hermes-voice-agent-plan]]。
