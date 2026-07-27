---
name: hermes-session-lifecycle
description: 网关按 session id 存对话历史 —— 「新对话」是换 id 不是删旧的，id 必须落盘；会话不换名会一直长，本地大脑每轮重嚼
metadata:
  type: project
---

Hermes 网关把对话历史按 **session id** 存在 `state.db`（SQLite），每轮从库里读，
**不缓存 per-session agent**。所以「让大脑忘掉」这件事完全由 id 决定。

**实锅（2026-07-26 发现）**：voice 会话从 2026-07-19 建起就没换过名，累计
`message_count 305 / api_call_count 399 / input_tokens 2.3M`。而
[[local-llm-brain-mac]] 已经实测过**本地模型 prompt 长度就是延迟**——不换名不只是
「记忆没清」，是在给每一轮加钱。GUI 的「新对话」按钮当时对 hermes 直接早退
（只有 omni 分支真去 reset），按了等于没按。

**做法**：新对话 = 建 `voice_<YYYYmmdd_HHMMSS>` 新会话并切过去，**不用
`DELETE /api/sessions/{id}`**——删除会把真实对话记录从库里抹掉，为了表达「开始新的」
而销毁数据；换名字同样零历史，旧 transcript 还能翻。

**两个非选项**：
- **当前 id 必须落盘**（`~/.config/lekiwi/hermes_session`，一行文本，不进 config.json
  ——那是配置，这是运行态）。不落盘的话 daemon 一重启就退回那个被要求忘掉的旧会话，
  「新对话」变成一次性的谎。写盘失败要在响应里说出来，不能静默。
- **`/health` 要报当前 session id**。会话会换名，不报出来就没法回头翻这段 transcript。

**验过的网关原语**：`POST /api/sessions {id}` → 201（重复同 id → 409）；
`DELETE` → 404 后可再 POST 重建；`GET /{id}/messages` 数 `role=="user"` 才是「几轮」
（`message_count` 把 assistant/tool 都算进去）。**新会话的 `system_prompt` 是空的，
但人格不受影响**——它来自 profile 的 SOUL.md，不是会话行；实测新会话第一句
「你是谁」照样答「我是小黑车」。探针建的临时会话也是裸建。

相关：[[hermes-brain-switch-probe]]、[[local-llm-brain-mac]]、[[agent-voice-pages-plan]]。
