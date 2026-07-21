---
name: voice-asr-engines
description: 语音 ASR 引擎阵容 + 流式模式:qwen3-asr 抗噪主力、四离线+四流式可热切换、两级选择;加/换 ASR 前必读
metadata:
  type: project
---

voice-daemon 的 ASR 做成**数据驱动多宿主 + 流式模式**(2026-07-21)。改/加 ASR 前读这条,别重复踩坑。

**离线引擎**(`voice_engines.REGISTRY["asr"]`,VAD 切段→`transcribe(whole_seg)`,进程内热切换,载新→卸旧):
- `qwen3` = **Qwen3-ASR-0.6B int8,sherpa 原生 `from_qwen3_asr`**(板上 sherpa 1.13.4 就有,不用 torch/GPU)。LLM-ASR,**带噪鲁棒性碾压小模型**(0dB WER ~7% vs SenseVoice ~13%),延迟 ~1.5s/短段。**RSS 随音频长度涨**:1-3s 段 ~0.9-1.2GB,20-50s 长音频才飙 3.5GB(KV cache);decoder `max_total_len=512`,>10s 会截断。**这是抗噪主力/默认**。
- `sensevoice`/`paraformer` = NAR 小模型(~230MB, <1s),多语种(sensevoice zh-en-ja-ko-yue),但**近场噪声下都马马虎虎**,实测半斤八两。
- `whisper` = large-v3-turbo int8(~1GB, RSS 1.4G),**RTF~2 太慢**(2s 音频要 4.3s),淘汰。

**流式模式**(`voice_config` stream 轴 `{enabled,model,endpoint_silence_s}`,`StreamingAsr`+`STREAM_SPECS`,`OnlineRecognizer.from_transducer`,**免VAD 自带端点**,`feed()→(partial,final)`,DEBUG-only 不进对话链路)。二级可选模型:
- `zh-2025`(默认,快 RTF0.13)、`zh-xlarge`(700M 更强但**daemon 里载入 ~24s**、纯中文)、`multi-zh`(14k小时)、`zh-en`(弱双语,唯一能中英混)。
- **流式 zh 模型全是纯中文**,不认英文;**流式精度天生低于离线**(会重复字)。**中英混说该走离线 qwen3**。
- **现场实测(2026-07-21,MCP01 环境):xlarge 勉强能识别一些,zh-2025/multi-zh(300/200M)几乎全失败、不可用**。原因叠加:流式本就弱 + MCP01 丢音频 + 流式对丢块更敏感 + 用户反馈"识别一句后忽略几句"(间歇丢音频特征)。**结论:这套硬件上流式基本是对比工具,不是可用模式;可用路径是离线 qwen3**。换好麦后是否可用需重测。
- 流式切换**后台加载**(大模型同步会超 GUI HTTP 超时):HTTP 立即返回 `state:loading`,GUI `confirmStreamSwitch` 轮询 `/health.stream.loaded`。

**GUI 两级**:一级`识别模式`(VAD+离线/流式)→二级`模型`(随模式变)→参数随模式显隐(VAD 参数 vs 端点静音),增益全局。`voicelab.js` `fillModelSel/applyModeUI`。段级「🔁 重识」= `/asr_debug/seg_asr` 同段换引擎对比。

**回头待办(用户 2026-07-21 提)**:给 xlarge 流式**提速**再头脑风暴——方向:①GPU(sherpa
`from_transducer(provider="cuda")`,但要 onnxruntime-gpu-for-jetson + 抢 VLM 显存)②加线程
(现 num_threads=4,Orin 6 核可试更多)③fp16(GPU 上比 int8 快;有 fp16 包)。目标是让流式
解码跟上实时不丢块(内联解码太慢是"忽略几句"的一因)。载入 24s 是另一回事(一次性)。

**调研已否掉的**(codex+kimi,别重查):nemotron-3.5-asr 有中文但**只出 QNN/高通版**(Jetson 跑不了);nemotron-speech CPU 版是**纯英文**;Qwen3-ASR-1.7B 无 sherpa int8、CPU~4-5s、GPU 要 torch 服务抢显存 —— **都不值**。真瓶颈是音频 SNR,不是模型。相关:[[voice-frontend-s2]] [[voice-venv-dual-sherpa113]] [[board-memory-ceiling]]
