# Memory Index

按需打开：读这里的一行钩子判断相关性，再打开对应文件。协议见 `SKILL.md`。

- [Jetson 平台基线](jetson-platform-baseline.md) — Orin Nano 8GB (SKU 3767-0005) + 256GB NVMe + JetPack 6.2.2，选容器/轮子前先看
- [JetPack 版本选择](jetpack-version-choice.md) — 为何用 6.2.2 而非最新 7.2：生态成熟优先于版本新
- [板子 apt 国内网络](jetson-apt-network-cn.md) — 装任何 apt 包前：换 TUNA 镜像 + 强制 IPv4 + hold nvidia 包
- [ROS 2 Humble 已装](ros2-humble-installed.md) — 板子已装 humble desktop，SSH 免密已配，直接 ssh jatson@192.168.3.188
