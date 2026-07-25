---
name: vad-asr-autotune-plan
description: 待做实验——Mac 跑 Matcha 发声、车子跑 ASR、多轮自动扫参得最佳 VAD/ASR 配置；含已测出的关键事实与三次测量失败的教训
metadata:
  type: project
---

**待做（2026-07-25 记录，compact 后继续）**：搭一个自动调参实验——
**Mac 用 Matcha 合成语音播放 → 车子麦克风收 → 板上 VAD 截段 + ASR 识别 →
多轮自动扫参 → 得出最佳 VAD/ASR 配置**。

起因：omni 大脑上线后误触发极严重，机器人不停对着环境噪音自言自语。
详见 [[omni-brain-two-stage]]。

## 已测出的关键事实（别重新推导）

**`fsmn` 引擎忽略一半参数。** `voice_vad.py` 的 `make_vad()`：
`return FsmnVad(msp, pr)` —— **`threshold` 和 `min_silence_s` 根本没传进去**，
类 docstring 也写明 "threshold is ignored (FSMN has its own internal state machine)"，
且 **FSMN 自带约 150ms 前导**。所以 fsmn 下真正可调的只有：
- `min_speech_s`（当时 0.1，过松，是压误触发的**唯一**旋钮）
- `pre_roll_s`（当时 0.9，在 150ms 自带前导之上再加，过量）
- `audio.gain_db`（当时 **+15dB**，在 VAD 和电平表**之前**施加）

要让阈值/尾静音生效，得换 `silero` 或 `ten` 引擎。

**现场实测数据**（板上 MCP01 麦克风）：
- 本底噪声（+15dB 增益下）：中位 **-42.6 dBFS**，p10 -47.8，最大 -18.8
- **无人说话时 VAD 有 47% 的采样判定为语音** ← 这是误触发的直接原因，正常应 <5%
- 真人一句话时长 ≥2.2s；误触发段多在 0.46–0.65s
- 段时长分布（10 段样本）：中位 2.24s，p25 1.16s，最短 0.52s

**调参该用 DEBUG 转写台**：`POST /asr_debug {on:1}` 进入后
**只截段+识别，不进大脑、不触发播报**。机器人全程不出声，
测量就不会被它自己的回声污染。增量读 `GET /asr_debug/tail?since=<seq>`。

**Matcha 资源**：板上有 `voice/models/matcha-icefall-zh-en`(93M) +
`vocos-16khz-univ.onnx`(52M)；**Mac 上没有 sherpa_onnx，也没有模型**。
两条路：(A) 板上一次性生成语料 → 拷到 Mac → Mac 播放（无需给 Mac 装依赖，
且生成与测试解耦）；(B) Mac 装 sherpa-onnx + 拷 145M 模型。用户原话倾向 B。

**已写好的脚手架**：`scripts/vad_tune.py` —— Mac 播固定脚本、板子在 DEBUG 台截段，
按 `min_speech_s × gain_db` 扫参，统计「多出的段数（误触发）」与「出字段数（真话保留）」。
当时用 macOS `say` 发声，要改成 Matcha。**未跑通过，需要先验证再信它的输出。**

## 三次测量失败的教训（同一天连踩三次）

见 [[measurement-validity]]。具体到这个实验：
1. **VAD 只在 LISTENING 状态运行**。daemon 处于 `idle` 时 `_handle_chunk` 直接
   `vad.reset()` 返回，`vad_active` 恒为 False —— 在 idle 下测出来每组都是 0%，
   看起来「参数全都完美」。测量前必须断言 `state != idle`。
2. **环境噪声会漂移 30dB**。在活麦上顺序 A/B 不同参数，比的是环境不是参数——
   实测出现过 threshold 0.7 比 0.5 「更差」这种物理上不可能的结果。
   **必须同一段固定音频**（离线回放，或 Mac 播固定语料）。
3. macOS 新的中文神经语音（Flo/Eddy 等）**接受 `-o` 但静默写出 ~0.02s 的桩**。
   用 `Tingting`，并断言时长。

## 验收该看什么

同一套播放语料下：
- **误触发段数 → 0**（静音间隔里不该截出任何段）
- **真实语句的出字段数保持满分**（收紧不能把「停」这类短指令吞掉）
- 两者是一对权衡，要的是帕累托前沿上的点，不是单看某一个

相关：[[omni-brain-two-stage]] [[voice-asr-engines]] [[voice-frontend-s2]]
