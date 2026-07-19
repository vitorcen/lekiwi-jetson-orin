---
name: voice-frontend-s2
description: S2 语音前端落地 — SenseVoice+edge-tts/Melo 选型(推翻 faster-whisper)、MCP01 必须开机的坑、句子累积器边界规则
metadata:
  type: project
---

2026-07-19 S2 语音前端上线(`voice/daemon.py`,systemd user 服务 voice-daemon,:8092
token 鉴权,GUI 走 Rust 代理)。codex gpt-5.6-sol 调研后**推翻了 faster-whisper 预案**:

- **ASR: sherpa-onnx SenseVoice-Small INT8**(非自回归,3s 音频解码 0.2s,~450MB,
  中英混说+标点 use_itn);faster-whisper small 要 ~1GB 且慢数倍,8GB 板不划算。
- **TTS: edge-tts 小晓主选**(首包实测 ~1.4s,超时阈值必须 ≥2.5s,1.0s 会永远降级)
  + **sherpa-onnx Melo zh-en 本地降级**(熔断 3 败切 300s);piper 仅二级保底。
- **VAD: Silero**(sherpa-onnx 内建),尾静音 0.55s;唤醒词不上,按键+常开窗口
  (默认 8 分钟,每轮续期,GUI 可调)。
- 三模型常驻 RSS ~800MB,MemoryHigh=900M。半双工闭麦,播完 250ms 恢复;
  打断=杀 aplay+清队列+撤 SSE+generation++,实测到静音 0.28s。

**坑(最重要)**:thinkplus **MCP01 是带电池的便携会议音响——插 USB 只是充电待机,
扬声器能响但麦克风 DSP 不上电,采集输出纯数字零(rms=0)**。必须**长按电源键 3 秒开机**
(环形灯全灭=没开机;红色常亮=静音键静音)。拔 USB 线不能重启它(电池撑着)。
另:声卡上电默认音量 29%(-20dB),daemon 在 discover_audio 时自动 amixer 拉到
播放 90%/采集 85%(ALSA 状态不跨重启,alsactl store 要 root)。设备无公开 API,
仅 USB 声卡+HID 按键接口(静音/音量/摘机上报,LED 输出位,报告 ID 3)。

**句子累积器边界规则**(修过一次斩腰 bug):标点绝对优先——硬边界。!?;首段≥2字
即切(抢首音),软边界逗号首段≥8/后续≥16;强切(首段24/后续42)前先回找最近标点,
切点吸收尾随标点+引号,段首孤儿标点丢弃。Hermes SSE 的 tool.progress(_thinking)
也带全文 delta,只有 assistant.delta 才能喂 TTS,否则重复播报。

**无声故障(2026-07-19 二锅)**:MCP01 拔插后 `_capture_loop` 只翻 `audio_ok=True`
不更新卡名 → `audio_ok=True` 但 `cap/play_card=None` 死角,watch loop 被短路,
aplay 打 `plughw:CARD=None` 秒退,edge 断管全"失败"、melo 不查返回码报"成功"
→ **无声且不报错**。修复:capture 重启走完整 `discover_audio()`;watch loop 检
卡名失一致(故障期 5s 巡检);两通道都查 aplay 返回码,非零 → `audio_ok=False`
上报重发现(被打断 kill 的非零码用 gen 区分,不算设备故障);`_synth_and_play`
播前发现设备不 ok 当句重发现。原则:**播放子进程返回码必须检查,"成功"必须是真的**。

**打断模式(barge-in,2026-07-19)**:实测 **MCP01 硬件 AEC 只有部分抑制**
(1kHz 探测音在采集里仍清晰可检),纯 VAD 开麦必被自己播报误触发。方案 = SPEAKING
不闭麦 + 三重门限:段时长≥0.55s → RMS≥0.02 能量门 → **SenseVoice 快解码后与近期
播报句(_recent_tts 环形 8 条/20s 窗)做 difflib 相似度比对**,≥0.55 或互为子串判回声
丢弃;命中停止词(停/别说了…,绕过 2 字下限)只打断不起轮;其余真插话 → 打断并把
识别文本直接作为新一轮输入。`VOICE_BARGE_IN=0` 可退回半双工。长文本自回声压测:
4 句完整播完 0 误触发。

**实测延迟**:说完→首音 P50 ~2-4s,大头是 DeepSeek 云端首 token(1.8-6.8s 波动),
板上环节(VAD 0.55+ASR 0.2+edge 1.4)已压实。相关:[[hermes-voice-agent-plan]]、
[[vlm-stack-orin]]、[[drive-mcp-skill]]。
