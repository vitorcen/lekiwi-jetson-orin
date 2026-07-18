# Memory Index

按需打开：读这里的一行钩子判断相关性，再打开对应文件。协议见 `SKILL.md`。

- [Jetson 平台基线](jetson-platform-baseline.md) — Orin Nano 8GB (SKU 3767-0005) + 256GB NVMe + JetPack 6.2.2，选容器/轮子前先看
- [JetPack 版本选择](jetpack-version-choice.md) — 为何用 6.2.2 而非最新 7.2：生态成熟优先于版本新
- [板子 apt 国内网络](jetson-apt-network-cn.md) — 装任何 apt 包前：换 TUNA 镜像 + 强制 IPv4 + hold nvidia 包
- [ROS 2 Humble 已装](ros2-humble-installed.md) — 板子已装 humble desktop，SSH 免密已配，直接 ssh jatson@192.168.3.188
- [舵机总线 bring-up](servo-bus-ch341-bringup.md) — CH340 需自编 ch341.ko + 卸 brltty；舵机 Feetech STS3215 ID1@1Mbps，控制脚本在板子上
- [目标机器人 LeKiwi](lekiwi-robot-target.md) — 3轮底盘+SO-101 从臂 9电机单总线；lerobot 锚定 26ff40d；语雀资料可走 API 匿名读
- [板子已装 lerobot](lerobot-installed-orin.md) — conda env lerobot/py3.12；GitHub 必须走 ghfast.top 镜像；PyPI torch 是 cu130 用不了 GPU
- [LeKiwi 控制台 GUI](lekiwi-gui-tauri.md) — gui/ Tauri 桌面端；ZeroMQ Tab 键盘遥控已成；ZMQ 只能在 Rust 后端；改遥控前必看
- [LeKiwi 手柄遥控](lekiwi-pad-teleop.md) — 板载 systemd daemon evdev→ZMQ 开机自启；evdev 轴必须 absinfo 初始化否则满偏自走
