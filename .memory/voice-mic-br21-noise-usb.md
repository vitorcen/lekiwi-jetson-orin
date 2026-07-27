---
name: voice-mic-br21-noise-usb
description: 车上麦是 JieLi BR21 不是 MCP01；底噪 78% 在 100Hz 以下，宽带 RMS 测的是隆隆声不是语音；BR21 与舵机串口共享 Single-TT hub；调 VAD/能量门/查丢音前必读
metadata:
  type: project
---

2026-07-27 在板上实测（idle、麦克风空闲，48k/16bit 原生录 6s）。**改 VAD 阈值、加能量
判据、或排查「识别一句丢几句」之前先读这条**——多半不是模型问题。

**硬件本体是好的**：`JieLi BR21 (e5b7:0811)`，L/R 逐位相同（单麦复制成立体声），
DC −0.0002，3038 个不同取值、精确零仅 0.2%（**没有数字静音门**），无死频段。
采集增益已顶 `amixer 'Mic' 127/127 (+23.81dB)`，**没有余量，别再想调大**。
开头 0.23s 有一次 −10.4 dBFS 启动爆音（一次性）。

**① 底噪 78% 在 100Hz 以下**（总 −39.3 dBFS）：0–100Hz 占 78.1%、100–200Hz 6.4%，
而 300Hz 以上只有 −55 dBFS 左右——**语音频段其实很干净**。谱峰在 18–40Hz 一片 +
60Hz/100Hz 电源线频；风扇 pwm=100/255 在转。150Hz 高通把底噪压到 −47.2（**白赚
8 dB**，120Hz 也有 7.3 dB），而 150Hz 以下对识别几乎无信息（电话带宽 300–3400Hz）。

**② 所以宽带 RMS 测的是隆隆声，不是语音。** 电平表、段 RMS、VAD 吃的都是宽带信号，
被 <100Hz 主导，「有人说话」和「没人说话」算出来差不多。这一条解释了三件旧事：
barge-in 那道 `BARGE_MIN_RMS=0.020(−34dBFS)` 能量门实测 **6/6 真人语音段全挡**
（段 RMS −35~−39，和底噪 −38 挤在一起）；VAD 阈值 0.3 抓不到、0.15 又误触发；
电平表读 −38 让人误以为「麦克风太弱」。**不是阈值调不对，是被测的量里 78% 与语音无关。**
另：播报时麦克风读到自己是 **−5~−17 dBFS**，比人说话（峰值 −22~−28）还响 15~20 dB
——所以那道能量门从来没挡住过任何回声，只挡住了安静的真人，方向完全反了（已删）。

**③ USB 拓扑：BR21 与舵机串口共享一个 Single TT。**
`Realtek 0bda:5489 (multi-TT) → 华升 214b:7260 (**Single TT**) → {BR21 音频, CH340,
CDC-ACM ttyACM0, Logitech HID}`，全是全速(12M)设备。等时音频每 1ms 要保留带宽，和
串口/HID 抢同一个 TT = 间歇丢音频的教科书配置。**修法是物理的**：把麦克风挪到外层
Realtek hub（TT per port）或直插板子。另：`base_host` 曾崩溃重启 208 次
（`wheel 7 did not answer`，轮子没上电），每 3s 敲一次同 TT 上的串口，会加重这个。

**④ 代码里的 MCP01 保活是死代码**：daemon 按 vid:pid `17ef:a03b` 找 hidraw 发 off-hook，
车上根本没有该设备（静默 return None，无害）。[[voice-frontend-s2]] 里「MCP01 必须长按
电源键否则麦克风全零」「MCP01 带硬件降噪门」是**上一只麦克风**的结论，对 BR21 不成立。

相关：[[voice-frontend-s2]]、[[vad-asr-tuning-results]]、[[measurement-validity]]、
[[voice-asr-engines]]、[[agent-voice-pages-plan]]。
