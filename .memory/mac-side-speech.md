---
name: mac-side-speech
description: Mac 侧语音工具链（电脑播报 TTS / 电脑 ASR）：板子没外网所以 edge 只能在 Mac 跑；HF 大文件在这台机上不可用要走 ModelScope；f5 时长必须按参考语速推
metadata:
  type: project
---

2026-07-26 落地。Mac 侧语音工具在 `scripts/mac_tts_render.py`（电脑播报的 edge/f5 渲染）
和 `scripts/mac_asr_server.py`（电脑 ASR 服务），GUI 侧入口是 Voice 页的「电脑播报」台
和 ASR 转写台里的 remote 引擎。实现细节看代码注释，这里只记代码里看不出来的环境事实。

## 板子没有外网（决定了整条链路怎么分工）

实测 `speech.platform.bing.com` 和 `www.microsoft.com` 从板上都是 `code=000`。
所以 **edge-tts 在板上永远不可用**，daemon 会静默回落 Melo，表现成「换 edge 音色不生效」。
`/health.edge_breaker` 早就报了这件事，GUI 现在会把它画出来。

**How to apply:** 需要 edge 音质的场景（调参语料、播报）一律放 Mac 侧；板上 TTS 只考虑
matcha/melo。看到「edge 换音色没反应」先看 `edge_breaker`，别去查切换逻辑。

## 这台 Mac 拉 HuggingFace 大文件不可用，要走 ModelScope

同一个 `flow.pt`：`huggingface.co` **55 KB/s**、`hf-mirror.com` 连不上、
`modelscope.cn` **9.0 MB/s**（快 163 倍）。CosyVoice2 5.4GB 走 ModelScope 7分56秒。

附带坑：下载进程被杀后会留下 `.lock` 文件，之后每次重试都卡在
"Still waiting to acquire lock" 且**一个字节都不动**，看起来像网慢。先删锁再重试。

**How to apply:** 凡是国内有源的模型（阿里系、魔搭有镜像的）优先 ModelScope。
`mac_asr_server.py` 默认的 `mlx-community/whisper-large-v3-mlx`（3GB）大概率撞同一堵墙。

## f5-tts 的时长必须按参考音频语速推

三种给法实测：不给 → 0.08s 静音；`estimate_duration=True` → 短句崩（1字 −92dBFS、
2字 −42dBFS）；手拍 0.32s/字 → 长句被拉伸，**听感明显断字**。
按参考自己的语速（`ref_秒数 / len(ref_文本)`）推，1~13 字全程 0.18–0.23s/字。
细节和数字在 `scripts/mac_tts_render.py` 的 docstring 里。

edge 输出**自带头尾静音**（「停」1.78s 里只有 0.4s 是人声）。播报台的句间停顿是测 VAD 时
的自变量，不裁剪等于每个停顿都偷偷加 1.4 秒 —— 渲染器统一裁到语音区间。见
[[measurement-validity]]、[[vad-asr-tuning-results]]。

## GUI 是 .app，shell 调用必须显式拼 PATH

launchd 给的是最小环境，`sh -lc` 只读 `~/.profile`（用户 PATH 在 `~/.zshrc`），
干净环境下 `uv`/`hf` 都找不到。`env -i` 复现过。main.rs 的 `sh_env()` 显式拼
`~/.local/bin` 等标准目录。

## CosyVoice2 在 Apple Silicon 上：能装，但我这次没跑对

装的坎（都已验证可过）：`pynini` 无 arm64 wheel → `brew install openfst` 后源码编译
1分40秒；`ttsfrd` ARM 上没有 → 用 `WeTextProcessing`；setuptools 83 删了 `pkg_resources`
→ 钉 `setuptools<81`；torchaudio 2.13 走 torchcodec → 换 soundfile；repo HEAD 的
`prompt_wav` 收**路径**不收 tensor（老教程全是 tensor）；`llm.pt` 是 bf16 而 CPU 激活
是 fp32 → 要 `.float()`。没有 MLX 移植，只能 torch，Mac 上跑 CPU，RTF ≈ 1.0–1.8。

**结果没跑对**：输出时长恒为 1.60s 的整数倍（4.80/11.20/1.60），换完全不同的参考音频
一秒不变，听感像胡说 —— 大概率是那个 fp32 强转破坏了 LLM 的停止条件。
**这不是 CosyVoice2 的水平评价**，是我环境的问题，别引用。要复现得先查它的采样/停止条件。

**How to apply:** 想再评 CosyVoice2 就从「为什么输出长度被量化成 1.6s」查起，
别从头装一遍 —— 装的部分是通的。
