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

**实测延迟**:说完→首音 P50 ~2-4s,大头是 DeepSeek 云端首 token(1.8-6.8s 波动),
板上环节(VAD 0.55+ASR 0.2+edge 1.4)已压实。相关:[[hermes-voice-agent-plan]]、
[[vlm-stack-orin]]、[[drive-mcp-skill]]。
