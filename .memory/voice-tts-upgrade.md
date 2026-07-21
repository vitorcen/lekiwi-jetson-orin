---
name: voice-tts-upgrade
description: 离线 TTS 升级调研(2026-07-22 板上实测):melo/kokoro CPU 不实时,matcha-zh-en RTF 0.18 是低延迟赢家;GPU 只对 Qwen3-TTS 质量线有意义
metadata:
  type: project
---

离线 TTS 升级调研(2026-07-22,codex+kimi 双路头脑风暴 + 板上实测)。改 TTS 前读这条。

**板上实测 RTF(Orin Nano CPU,同一句 zh+en 混合测试句)**:
- melo(现役离线):2线程 **1.60** / 4线程 1.07 —— **不实时**,4.5s 音频要 7s;
  且 lexicon 会**直接丢阿拉伯数字**("3""15" OOV ignore)。离线兜底其实一直是坏的。
- kokoro-multi-lang v1.1(103 音色 zh-en):fp32 2线程 2.25 / 4线程 1.44;**int8 更慢 2.33**
  (StyleTTS2 图在 ARM 上 int8 无收益)。CPU 路线死;模型已留板上 models/。
- **matcha-icefall-zh-en + vocos-16khz-univ:2线程 RTF 0.18(8×实时),载入 3.5s,
  模型 93MB+52MB** —— 低延迟赢家,sherpa 1.13.4 原生 OfflineTtsMatchaModelConfig,
  自带 number/date-zh.fst 归一化。坑:概率性合成错误有 open issue;单音色;
  必须配 16khz vocoder(22khz 会告警且音质错)。样音已给用户 A/B。

**质量上限线(GPU 才有意义)**:Qwen3-TTS-12Hz-0.6B-CustomVoice(2.5GB fp16,官方只有
torch/vLLM,无 ONNX/GGUF 上游)。**Jetson 现实路径 = NVIDIA TensorRT-Edge-LLM**(官方支持
0.6B,拆 Talker/CodePredictor/Code2Wav 三引擎;社区有 int4+fp8 现成包
harvestsu/qwen3-tts-0.6b-customvoice-jetson-trtllm-int4fp8,~1.7GB,9 音色 zh+en,原生流式;
Edge-LLM issue #87 有 Orin Nano 8GB 同板跑 ASR+TTS 先例)。第三方 GGUF fork
(HaujetZhao/Qwen3-TTS-GGUF、qwen3-tts.cpp)可复用我们 llama.cpp 经验但非上游。
坑:Predictor 每帧串行 15 次是天然瓶颈;llama.cpp sm_87 flash-attn 会崩(须关 FA);
TensorRT-LLM≠Edge-LLM(前者 Jetson 不可用);官方 97ms 是云端数,板上预期 ×3-5。

**已否掉**:CosyVoice3-0.5B(FunAudioLLM,ONNX 要 5.2GB 显存/无 Jetson 支持/ttsfrd 仅
x86,8GB 板出局)、IndexTTS-2(4-8GB)、FishAudio S1-mini(非商用)、OuteTTS(中文差)、
Piper(机械感,中英不能混读)。ZipVoice zh-en(123M,克隆)未测,需要克隆时再说。

**GPU 收益评估结论**:matcha CPU 已 8×实时 → 对小模型 GPU 无意义(codex 判定一致);
sherpa-onnx 官方现已支持 Orin Nano JetPack 6.2 GPU 自编(ORT 1.18.1+CUDA 12.6,板上正是
12.6)——**之前"cuda wheel 绑 CUDA 10.2"的 GPU 否决理由已过时**,但预编包仍是 JetPack 5
时代的,要自编;只有 kokoro 音质路线或重开 ASR GPU 时才值得动。GPU 真正的用武之地是
Qwen3-TTS 质量线。

**matcha 已集成为第三 TTS 引擎(2026-07-22,板上验证)**:`MatchaTts`(16k,按需载/卸,
离开即卸回收 ~150MB)+ `_local_play(host)` 泛化(aplay 采样率跟宿主);melo 仍常驻
(edge 兜底+错误短语)。选路:tts_engine=matcha 且已载→matcha,否则 melo 静默降级。
**matcha 是第一位/默认**(用户 2026-07-22 定谳:实时性好、音质还行):
TTS_ENGINES=["matcha","edge","melo"],DEFAULT_CONFIG pair tts={"engine":"matcha"},
板上两 preset 已落盘 matcha。GUI 枚举驱动免改。板上验证:切换/播报
backend=matcha/退调试还原+卸载。用户痛点:edge 在线"每几个字断1秒"(逐句网络延迟)
→ matcha 离线化是解法。同日修 hermes SSE 断轮实锅:aiohttp 行迭代器 512KB 上限,
assistant.completed 一行超限抛错 → `_iter_sse` 自行 iter_chunked 切行,无上限。
文档:docs/voice-asr-tts-engines.html(引擎全景+淘汰赛+实锅表)。

**后续待办**:② kokoro 音质路线要 sherpa GPU 自编(JetPack6.2+ORT1.18.1 官方已支持)→
③ Qwen3-TTS TRT-Edge-LLM 质量上限实验(先停 VLM 腾内存)。
相关:[[voice-asr-engines]] [[voice-frontend-s2]] [[board-memory-ceiling]]
