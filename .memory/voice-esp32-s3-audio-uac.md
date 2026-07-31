---
name: voice-esp32-s3-audio-uac
description: ESP32-S3 UAC 必须保持 24k USB 边界；AEC 内部 16k；Matcha 播放前显式重采样到 24k
metadata:
  type: project
---

2026-07-30，Waveshare ESP32-S3-AUDIO-Board（USB `303a:8211`）已改为标准
USB Audio Class 设备：麦克风 24 kHz/16-bit/mono，音响
24 kHz/16-bit/stereo。Jetson ALSA 卡名为 `Audio`，`arecord -l`/`aplay -l`
均显示 `LeKiwi ESP32 Audio`；voice-daemon 的通用 `USB Audio` 发现逻辑可自动选中，
不需要按卡号写死。

ES7210 与 ES8311 在这块板上共享同一组 BCLK/WS，必须跟微雪原厂实现一样使用
`2×32-bit standard I²S` 全双工。曾把播放配成 `2×16-bit standard`、录音配成
`4×16-bit TDM`；USB 虽能枚举且能收到测试音泄漏，真人裸录却只有 −85.5 dBFS、
18 个离散值。这不是低增益，而是两个主时钟格式互相打架。修正后 USB 接口仍保持
24 kHz/16-bit/mono 输入和 24 kHz/16-bit/stereo 输出：录音从两个 32 位槽的高
16 位取样，播放则把 USB 的 int16 左移扩成 codec 所需的 int32。

ES7210 ADC 增益最终设为芯片上限 37.5 dB。2026-07-30 同一半米现场、同一句 Jetson
Matcha TTS，30 dB 时 Mac 录音峰值 −31.4 dBFS，37.5 dB 时为 −24.0 dBFS，提升
7.4 dB 且无削顶。把这段真实空气传播录音送入 Jetson Qwen3 ASR，逐字识别为
“小乐你好，测试语音转写，机械臂已经准备好了。” 双麦的两个原始 32 位槽均已读入，
USB 暂只暴露第一路；双麦择优/阵列处理应在单麦基线稳定后用同一语料比较，不能直接
相加后假定识别率会提高。

最终接回 Jetson 后，voice-daemon 自动把 capture/playback 都选为 `Audio`。用 Voice
页同一套 `/asr_debug` 接口做 Mac 系统 TTS → 半米空气传播 → 新板麦克风 → Jetson
Qwen3 ASR，切段 3.61s、峰值 −22.8 dBFS、`accepted=1`，逐字识别“现在开始测试
机器人语音识别。” 测后转写台已关闭，daemon 回到 idle/bench=false。

USB 播放/录音边界必须保持 24 kHz；所有 16 kHz UAC 方案都造成严重断续，而原生
24 kHz 可连续播放。全双工回声消除使用 ESP-SR 2.4.6：在固件内部精确
24↔16 kHz 重采样，speaker reference 与 mic 送入 16 kHz AEC，USB 实时任务固定
core 0，AEC 异步任务固定 core 1。播放活跃时不得把来不及处理的原始 mic 数据泄漏
给主机，只能返回 AEC 输出或静音。现场自播末 8 秒从约 −21.5 dBFS 降到
−44.7 dBFS，原播报文本不再被识别；Fun-ASR 仍可能把低电平噪声幻觉成无关文字，
因此 GUI VAD 仍是必要的第二道门。

Matcha 输出固定 16 kHz。Jetson 若把它直接交给 ALSA `plughw` 实时转换到该声卡的
24 kHz，会出现重音/回声；直接 ALSA 24 kHz 和 Edge TTS 24 kHz 均正常。voice
daemon 仅在播放卡为 `Audio` 时，先把本地 TTS PCM 显式线性重采样到 24 kHz 再
调用 `aplay`。2026-07-31 现场复测暂未复现重音或断续。

原小智 AI 固件的 16 MiB 完整备份位于
`/Users/david/backup/esp32-s3-audio-original-20260730.bin`，SHA-256 为
`28edeac2811df8739ed4520463412839fef4b7f8f777ffb8fa54a6b071f1251e`。

**Why:** USB 能枚举、`aplay` 返回 0、甚至录到一点回授，都不能证明 ADC 总线格式
正确；真人采样的离散值数量与 dBFS 才揭示死输入。功放还需要 TCA9555 的 EXIO8
使能。

**How to apply:** 不得把 RX 改回 TDM 或让 TX/RX 使用不同位宽；排查时必须录真人
语音，正常近场峰值应明显越过 −25 dBFS。不得把 USB UAC 改成 16 kHz；AEC 的
16 kHz 只存在于固件内部。Matcha 播放必须在进 ALSA 前显式转成 24 kHz。恢复 AI
固件前先校验备份哈希。与 [[voice-frontend-s2]]、
[[voice-mic-level-cliff]]、[[measurement-validity]] 一起参考。
