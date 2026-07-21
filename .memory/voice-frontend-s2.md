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

**麦克风排查铁律(2026-07-20 踩满)**:三条互相独立、每条都单独浪费过时间——
① **不能用它自己的喇叭测它的麦克风**:MCP01 硬件 AEC 会主动抵消自身播报,实测放人声
时采集电平在 -30 ~ -75 dBFS 之间被剁碎,VAD 截不出完整段,ASR 必然不出字,**这不能
证明麦克风坏了**。② **ALSA `Mic Capture Volume` 对它是失效旋钮**:85% → 100% 本底纹丝
不动(rms 3.7 → 3.6),因为设备有硬件降噪门,静时直接输出近似数字静音(实测基线
-79 dBFS)。别再靠调 amixer 解决"声音小"。③ 因此 daemon 在 `_handle_chunk` 里(闸门
**之前**)算 RMS→dBFS,`/health` 暴露 `mic_dbfs`/`mic_peak_dbfs`(3s 峰值)——判读:
高于 -34 dBFS(即 `BARGE_MIN_RMS=0.02`)电平够,接近 -79 说明设备静音键被按了。
排查顺序应是:先读电平遥测 → 再用**已知波形直接喂 VAD+ASR**验证模型(实测能认)
→ 最后才怀疑声学。相关:[[board-memory-ceiling]]。

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

**MCP01 硬件不稳定 + P2.7 音频前端基建(2026-07-21)**:实测 MCP01 会**在 arecord
打开 PCM 采集的瞬间掉线**,随后只以 HID-only 重枚举(无 USB Audio 接口)甚至整机从
USB 总线消失,需**物理拔插/断电重上电**才回来——这就是"重插不恢复"的真因,是硬件不是
软件(daemon 重发现路径完好:回来后 discover_audio 会重新把 capture volume 拧到 100%
并重发现声卡,实测 `[100%]` 生效)。dmesg 显示自发 disconnect 早于 offhook 代码启用,
故 offhook 写不是根因。缓解三件套(均已落地,默认零行为变化):
① **摘机 keepalive**:向 MCP01 HID(vid:pid `17ef:a03b`,hidraw 按 vid:pid 定位不写死
——路径会从 hidraw2 漂到 hidraw0)写 output report `\x03\x01`(Report ID 3, bit0=Off-Hook),
采集期每 10s 重写,把话机拉出待机(待机麦增益掉 ~30dB)。需 udev 规则
`board/etc/udev/rules.d/59-mcp01-hid.rules`(MODE 0666)让 jetson 用户免 root 写;
装法 `sudo cp … && udevadm control --reload && udevadm trigger`。写失败静默跳过。
② **数字增益** config `audio.gain_db`(0~30,默认 0):`_handle_chunk` 归一化后、电平表
之前乘 `10**(g/20)` 并 clip [-1,1]——电平表显示增益后幅度(= VAD/ASR 实际听到的)。
实测(干净文件衰减+噪声模拟低幅采集):-40dB 时 gain=0 出 0 段、gain=+20 出段且识别,
-30dB 时 gain=0 掐头"今"、gain=+20 完整——**数字增益就是低幅采集的解药**,推荐起点 ~20dB。
③ **VAD 前置回溯 pre_roll_s**(默认 0.45,0~1.0):补"起音被掐头"。silero 侧 daemon 维护
滚动环,用 `SpeechSegment.start`(采样索引,reset 后近似归零、~512 样本量化)回取
`[start-pre_roll, start)` 拼段前;webrtc/energy 在聚合器开段时把 look-back 帧纳入。
`pre_roll_s=0` 复现改造前逐字节输出。

**VAD 可切换(P2.7)**:`voice_vad.py` 四引擎统一 `feed(f32_16k)->list[seg]`:silero
(现役,sherpa VoiceActivityDetector 包一层)、ten(sherpa 需带 ten_vad;板上 sherpa
已升 1.13.4,**ten 现已 available=true**,见 [[voice-venv-dual-sherpa113]])、webrtc
(py-webrtcvad,已在 venv 装成;注意
setuptools≥81 删了 pkg_resources 会让 webrtcvad import 失败,板上降到 `setuptools<81`
=80.10.2 修好,core 全链回归通过)、energy(纯 dBFS 门,零依赖,调试基准)。切换走
现有 switch 执行器(202+job+锁),ephemeral 与 tts 同语义(退 DEBUG 还原)。selftest
走当前选中引擎并回报 `vad_active_seen`。`/health` 加 `vad_active`(圆点)。板上四引擎
selftest 全 pass(ratio 0.933),升级 sherpa 1.13.4 后枚举可用性
silero/energy/webrtc/**ten** 全 =true(详见 [[voice-venv-dual-sherpa113]])。
