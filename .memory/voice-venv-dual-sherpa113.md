---
name: voice-venv-dual-sherpa113
description: 板端 voice 双 venv 布局(.venv 软链 → .venv-exp/1.13.4 vs .venv-stable/1.10.46)+ sherpa 升级启用 TEN VAD;含 TEN vs Silero 低幅对比
metadata:
  type: project
---

2026-07-21 把板端 voice(`~/work/lekiwi-jetson-orin/voice/`)的 sherpa-onnx 从
**1.10.46 受控升到 1.13.4**,启用 TEN VAD。用**双 venv + 软链**做秒级回滚:

- `.venv` 是**软链**(不是目录):`.venv -> .venv-exp`(现役常态)。
- `.venv-exp` = sherpa-onnx **1.13.4**(拆成 sherpa-onnx + sherpa-onnx-core 两个
  wheel,pip 自动拉 core;`VadModelConfig.ten_vad` 存在 → TEN 可用)。
- `.venv-stable` = 原 **1.10.46** 基线(**无** ten_vad),留作回滚垫。
- systemd user 服务 `voice-daemon` 的 ExecStart 指 `.venv/bin/python`,只认软链,
  切 venv 不用改 unit。
- **回滚一行**:`ln -sfn .venv-stable .venv && systemctl --user restart voice-daemon`;
  前滚:`ln -sfn .venv-exp .venv && …`。两个 venv 各 ~145MB。

依赖清单同 `setup.sh`(TUNA 源、sherpa `--only-binary` 只装轮子不编译、webrtcvad 需
`setuptools<81`=80.10.2 修 pkg_resources)。TEN 模型 `models/ten-vad.onnx`(~324KB,
文件名必须叫 `ten-vad.onnx` 对上 `voice_vad.TEN_MODEL`)从
`github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.onnx` 走 ghfast.top
镜像下载(注意是 `asr-models` tag,不是 `vad-models`——后者 404)。`models/` 被两个
venv 共享,只下一份。

**升级验证结论**:三模型(SenseVoice/Melo/Silero)在 1.13.4 下 API **零 breaking**,
`voice_engines.py`/`voice_vad.py` 无需改代码。全链回归通过:四引擎 selftest 全 pass
(ratio 0.933)、`/say` 200、`/simulate` 一轮(mimo 大脑)跑通。

**TEN vs Silero 低幅敏感度对比**(离线,selftest.wav 叠 -55dBFS 高斯噪底后逐级衰减,
不加数字增益,测 VAD 生裸信号灵敏度):
- **同阈值 0.35**:TEN 比 Silero 更敏感——语音衰到 SNR **-9dB**(-63.6 dBFS)TEN 仍
  出段,Silero@0.35 到 SNR -3dB 就掉段。TEN 多探进噪底 ~6dB。**这印证了换 TEN 的动机**
  (板子低电平采集时 TEN 更能截出段)。
- **各自默认阈值**(silero 0.35 vs ten **0.5**):反而 Silero 看着更敏感——纯粹因为
  ten 默认阈值更严,不是模型差异。**用 TEN 要把阈值调到 ~0.35 才发挥其低幅优势**。
- 干净无噪信号下两者都能一路衰到 -63dBFS 出段(无差异),差异只在有噪底时显现。

结束态:TEN 只是"可选亮了"(运行时探测 `availability()` 自动 available=true),
当前实际引擎仍是用户设的 **silero(threshold 0.35, gain 20)不动**,切不切用户在 GUI 点。

相关:[[voice-frontend-s2]] [[board-memory-ceiling]] [[lerobot-installed-orin]]
