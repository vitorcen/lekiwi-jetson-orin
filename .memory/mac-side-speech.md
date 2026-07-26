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

## 电脑播报→板子麦:识别率几乎只由**播出电平**决定(实测 3/10 vs 9/10)

2026-07-26 同一间屋、同一份归一化音频、前后 40 秒,只动音量旋钮:

| 系统音量 × 台音量 | 板子收到峰值 | 切出段 | 全对 |
|---|---|---|---|
| 69% × 70%(旧默认) | −24.7 dBFS | 10/10 | **3/10** |
| 100% × 100% | −16.8 dBFS | 10/10 | **9/10** |

**8 dB 决定 3/10 还是 9/10。** 调参语料当年落在 −9.3 dBFS,旧默认比它低 15 dB,
参数是在另一个电平上调的,照抄阈值毫无意义。低电平下 qwen3-asr 会**从噪声里编出通顺
整句**("有啊,有很大的,像三高中的那种"),和 [[vad-asr-tuning-results]] 记的 funasr
一个毛病 —— 所以"有输出"绝不等于"听见了",判据必须是逐字对。

三处电平在渲染器和 GUI 里已经修掉:渲染统一峰值归一化到 −1dBFS(音色不同响度差
2.6dB,不归一化时音量旋钮在每一行含义都不一样)、台音量默认 100、macOS 系统音量
读出来显示"合计 x%"并给一键拉满 —— **系统音量是这条链上唯一在 GUI 外面的乘数**,
不显示出来就会出现"我明明已经调到最大了"其实只有 69。

**剩下的 1/10 修不掉**:词首送气塞音,「停」→「平」、「帮我看一下」→「我看一下」。
就是 [[vad-asr-tuning-results]] 记的 MCP01 硬件降噪门吃词首起音,加电平不解决。

**How to apply:** 板子听不清先看转写台每段的「峰 xx dB」。**低于 −20dBFS 就别调
VAD 参数了**,先把电平找回来(音量/距离/音箱),否则调的是另一个工况。

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
