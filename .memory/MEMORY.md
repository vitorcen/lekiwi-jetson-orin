# Memory Index

按需打开：读这里的一行钩子判断相关性，再打开对应文件。协议见 `SKILL.md`。

- [Jetson 平台基线](jetson-platform-baseline.md) — Orin Nano 8GB (SKU 3767-0005) + 256GB NVMe + JetPack 6.2.2，选容器/轮子前先看
- [JetPack 版本选择](jetpack-version-choice.md) — 为何用 6.2.2 而非最新 7.2：生态成熟优先于版本新
- [板子 apt 国内网络](jetson-apt-network-cn.md) — 装任何 apt 包前：换 TUNA 镜像 + 强制 IPv4 + hold nvidia 包
- [板子 WiFi 6E AX210](jetson-wifi-ax210.md) — L4T 没编 iwlwifi，装 backport-iwlwifi DKMS；坑在 cfg80211 符号冲突，卸掉重载
- [ROS 2 Humble 已装](ros2-humble-installed.md) — 板子已装 humble desktop，SSH 免密已配，直接 ssh jatson@192.168.3.189
- [舵机总线 bring-up](servo-bus-ch341-bringup.md) — CH340 需自编 ch341.ko + 卸 brltty；舵机 Feetech STS3215 ID1@1Mbps，控制脚本在板子上
- [目标机器人 LeKiwi](lekiwi-robot-target.md) — 3轮底盘+SO-101 从臂 9电机单总线；lerobot 锚定 26ff40d；语雀资料可走 API 匿名读
- [板子已装 lerobot](lerobot-installed-orin.md) — conda env lerobot/py3.12；GitHub 必须走 ghfast.top 镜像；PyPI torch 是 cu130 用不了 GPU
- [LeKiwi 控制台 GUI](lekiwi-gui-tauri.md) — gui/ Tauri 桌面端；ZeroMQ Tab 键盘遥控已成；ZMQ 只能在 Rust 后端；改遥控前必看
- [LeKiwi 手柄遥控](lekiwi-pad-teleop.md) — 板载 systemd daemon evdev→ZMQ 开机自启；evdev 轴必须 absinfo 初始化否则满偏自走
- [Hermes 语音大脑方案](hermes-voice-agent-plan.md) — Hermes+deepseek-v4-flash 上板、自研 voice-frontend、受限 drive MCP、端侧 Qwen3-VL-2B；安全四层是核心，方案在 docs 经 codex 两轮评审
- [按需派 Opus 子代理](delegate-to-opus-agents.md) — 边界清晰的功能开发交给 Opus Agent，省主会话上下文
- [板上视觉栈已通](vlm-stack-orin.md) — llama.cpp sm_87 + Qwen3-VL-2B Q4 + vlm/ 三态省电 daemon + MCP + GUI Tab,改视觉先读 vlm/README
- [GS130WI 双目相机移植](rdk-gs130wi-camera.md) — CS130WI=GS130WI 双目模组（彩色 BGGR 非单色、无 IR-cut，X5 实测）；Jetson 无驱动，方案在 docs、实施在 dependencies submodule；EEPROM 出厂标定可复用
- [S2 语音前端已通](voice-frontend-s2.md) — SenseVoice+edge-tts/Melo,voice/ daemon+GUI 语音 Tab;MCP01 必须长按电源键开机否则麦克风全零
- [ROS 2 集成计划](ros2-integration-plan.md) — 2026-07-20 定稿经 codex 评审:并行层+安全门下沉 base_host+slam_toolbox/AMCL/DWB;动 ROS 前必读 docs/ros2-integration-plan.html
- [控车 MCP 已挂载](drive-mcp-skill.md) — drive/ 钳位 0.15m/s·2s,语音可驱动轮子;无人值守 gate 在 base_host v2;龙虾人格在 ~/.hermes SOUL.md
- [板端单测](unit-tests-board.md) — tests/ 纯逻辑单测,Mac 上 uv run --with pytest --with numpy pytest tests/ -q;只测纯函数不测胶水,JS 不测
