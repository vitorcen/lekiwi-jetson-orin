---
name: vlm-stack-orin
description: 板上视觉栈已跑通 — llama.cpp(571d0d54, sm_87) + Qwen3-VL-2B Q4 + vlm/ 三态省电 daemon + 只读 MCP + GUI 视觉 Tab
metadata:
  type: project
---

2026-07-19 S3 视觉栈端到端跑通(相机真帧中文解读,GPU 推理时 GR3D 90%+、空闲 1%):

- **llama.cpp** pin `571d0d54`(2026-07-18),`~/work/llama.cpp`,CUDA sm_87 Release。
  坑:8GB 板 `-j6` 编译把桌面挤崩(OOM),`-j4 + nice` 稳。CUDA toolkit 12-6 用
  pkexec 弹窗装(`pkexec bash -c 'apt-get install …'`,用户桌面授权,agent 无 sudo 可用)。
- **模型** `~/models/vlm/`:Qwen3-VL-2B-Instruct Q4_K_M(unsloth)+ mmproj Q8_0(ggml-org),
  SHA256 在同目录 SHA256SUMS。**不用 Qwen3.5-2B**:更强但 llama.cpp 视觉路径有活跃 bug
  (#19929 batch 相关静默乱答、#21268 CLIP 算子崩),等修稳后 A/B,切换成本≈0。
- **服务**(systemd user + linger,全自启):`llama-server.service`(:8091 lo,常驻显存,
  -ngl 99)← `vlm-daemon.service`(:8090,Bearer token 在 `vlm/token` 600)。
  代码在仓库 `vlm/`(daemon.py / mcp_server.py / install.sh / README)。
- **省电三态**:idle(不采集不推理、相机设备关闭、GPU≈0)/ watch(3s 间隔连续解读,
  无活动 90s 自动降 idle)/ describe 单发。权重常驻显存所以唤醒无加载延迟;
  实测单帧解读热身后 ~1.8s(调优:VLM_INFER_WIDTH=640 降采样送 VLM、max_tokens 80
  +一句话提示词、llama-server `-fa on`、nvpmodel MAXN_SUPER;调优前 4.6-6.6s)。
  相机原生 15fps@720p MJPEG。
- **MCP**:robot profile `mcp_servers.vlm` 三只读工具 vlm_look/vlm_last_caption/vlm_health,
  输出带 age_seconds + 不可信观测免责声明。**vlm_look 支持双相机(2026-07-22)**:
  `camera` 参数 front(头部,默认,连续采集+共享 caption)/ wrist(爪腕 Sunplus 2M,
  640x480,`_grab_jpeg` 一次性抓帧,永不缓存、不污染 front 的缓存/健康态,无共享
  caption——`/look` 对 wrist 恒走隔离 fresh answer)。板上验证:wrist 直拍 2.6s 出中文
  描述;LLM 经 vlm_look(camera=wrist) 全链路答对。注意 wrist 设备与 ROS wrist_cam
  节点按需共享,订阅期间抓帧会 EBUSY(诚实报错)。改 MCP schema 后须重启
  hermes-gateway-robot 才生效。
- **GUI**:📷 视觉 Tab,Rust ureq 代理(token 不进 WebView),帧 4fps/caption 1Hz 仅
  Tab 可见时轮询,进 Tab promote watch、离开发 idle(dead-man 同遥控 Tab)。
- 相机 /dev/v4l/by-id/usb-CN02KX4NLG…-video-index0,MJPEG 1280x720 抓帧(ffmpeg 单帧)。

**Why:** 三层解耦(引擎/采集调度/只读工具)+ 按需推理是 8GB 统一内存上「常驻但不耗」
的关键;caption 属不可信观测,安全边界见 [[hermes-voice-agent-plan]]。

**How to apply:** 改视觉栈先读 `vlm/README.md`;换模型只动 llama-server.service 的路径;
基准/压测(50 次静态图、ASR+VLM+TTS 联合)仍是 S3 gate 未竟项。
