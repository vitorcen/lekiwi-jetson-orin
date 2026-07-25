---
name: omni-brain-two-stage
description: omni 大脑（局域网 Qwen3-Omni）的核心设计——thinker 决策/talker 发声两阶段拆分，以及 P0/P1 的实测数字
metadata:
  type: project
---

2026-07-25 起做的「omni 大脑」：Agent 页面新增一个大脑种类，用户语音直接发到
局域网上 Mac 跑的 Qwen3-Omni-30B-A3B-Instruct-4bit（MLX），模型自己听/想/调工具/说话，
不经 TTS，本地 ASR 只负责把用户说的话变成文字给 GUI 显示。
设计文档：`docs/omni-brain-lan.html`。服务端代码在**另一个仓库** `~/work_ai/omnivla`
（`scripts/omni_two_stage.py` / `server.py` / `p0_bench.py` / `p1_client_test.py`）。

## 核心：两阶段拆分

`Model.generate(return_audio=True)` 内部是四步：① thinker 自回归 →
② `extract_thinker_hidden_states`（**对 prompt+生成结果的完整二次前向**）→ ③ talker → ④ code2wav。
**① 和 ② 之间有天然的缝**：thinker 文本在任何 talker 计算开始前就已完整可见。

在那里拆开：thinker 跑完 → 解析 `<tool_call>` → 有工具就**根本不进 ②③④**。
「工具 JSON 被朗读」「工具轮性能浪费」「两次生成的文本漂移」三个问题同时消失。

**Why 值得记：** 初稿方案是「永远 `return_audio=True`，发现 tool_calls 就丢弃音频」，
理由是「talker 开销正比文本长度，工具 JSON 只有几十 token 所以很便宜」。
**这个成本模型是错的**，被 codex 评审推翻并被实测证实：
② 的开销由**上下文长度**决定而非输出长度；而且 talker 在工具轮会**逐字朗读那段 JSON**，
反而比正常回答更贵——实测 8268ms。

## 实测数字（P0/P1 已 GO）

- 工具轮：**671ms**（两阶段）vs **4691ms**（初稿方案），中位省 **3949ms**
- 工具调用成功率 **100%**（`apply_chat_template(tools=)` + 纯音频输入在 mlx-vlm 上确实能吐 tool_call）
- 聊天首音：非流式 20.9s → 流式 `chunk_size=50` + 约束回复长度后 **2.6s**
- **流式是必需项不是优化项**；`chunk_size` 是首音延迟的旋钮（300→12.8s，150→7.3s，75→4.9s，50→2.6s），
  mlx-vlm 默认 300 对语音交互太大
- 第二个杠杆是**约束回复长度**（禁列表/禁 markdown/两句四十字内）——
  语音机器人吐带项目符号的列表本身就是产品 bug

## P2 现场踩到的坑（都已修，别再踩）

- **回声自打断**：`_recent_tts.append` 必须在**音频播放之前**登记（新增 `on_text` 回调）。
  放在整轮结束后，机器人会听见自己、判定被打断、起一轮撞上还在跑的旧轮（`omni busy`）。
- **打断没通知 Mac**：`alive()` 转 false 必须立刻 `DELETE /v1/requests/{id}`，
  否则服务端继续生成，下一轮必 503。
- **每块音频单独 spawn 一个 `aplay`** → 十几次进程启停，块间空隙听起来像回音。
  改成整轮一个 aplay、音频块写 stdin（`drain()` 背压）。
- **把工具响应整个 JSON 喂给模型**：`/caption` 返回带 `frame_b64`（整张 base64 JPEG），
  截断后是乱码 → 模型再试 → 打转到耗尽轮数。必须白名单投影字段（`_VLM_KEEP`）。
- **会话历史会自我强化复读**：噪音起轮 → 模型瞎编一句 → 进历史 → 下轮复读 → 再进历史。
  修法是重复时**两份都删**（留一份等于留着当初锁死的诱因）。
- **只出声不出字**：GUI 对话气泡渲染的是 `assistant_delta`，不是 `tts`。omni 无 token 流，
  整句作为一个 delta 发出。

## 「不是对我说的」哨兵

提示词让模型在听到噪音/回声/别人说话时**只输出 `<ignore>`**，服务端识别后
`status: "ignored"`——不进 talker、无音频、不进历史、不进对话记录，板上只累加计数。
两阶段拆分让这条路几乎免费（只跑 thinker，~600ms）。
**注意**：机制已验证可用，但模型的判断力是变量，合成语音上只挡住了一部分，
真实噪音效果需现场实测。根因仍是 VAD 误起轮，见 [[vad-asr-tuning-results]]。

## 「新对话」= 真的重置

GUI 按钮走 `POST /reset`(daemon) → `POST /v1/conversations/{cid}/reset`(Mac)，
清历史 + 清在途工具链；本机再记一个持久化的 `agentClearedSeq` 水位线，
否则 daemon 那 200 条事件环会在重连时把清掉的重放回来。
hermes 会话在网关侧，本按钮清不了——**如实返回，不假装成功**。

## 待办：P3（运动仲裁 + 切换）

P3 前有个**硬前置**：现在 drive/vlm 的运动互斥（`_motion_lock`）是**进程内**的，
hermes 的 MCP 子进程和 voice-daemon 各持一把，跨进程失效。
必须把工具业务逻辑抽成 import-clean 的共享模块（`drive/tools.py`），
互斥下沉到单一拥有者，**否则不许让车动**。
另注意 `voice/setup.sh` 没装 `mcp`/`pyzmq`，importlib 直接加载 MCP server 在 voice venv 里会炸。

相关：[[mlx-thread-local-stream]] [[hermes-voice-agent-plan]]
