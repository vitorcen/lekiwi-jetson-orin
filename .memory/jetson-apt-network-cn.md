---
name: jetson-apt-network-cn
description: 板子在国内网络 — apt 必须换 ubuntu-ports 国内镜像 + 强制 IPv4；升级前 hold 住 nvidia-l4t 包
metadata:
  type: feedback
---

Orin 板子（192.168.3.189）处于**国内网络**，装任何 apt 包前先处理两件事：

1. **换源**：arm64 的 Ubuntu 包在 `ports.ubuntu.com`，该域名国内 IPv4 超时、IPv6 unreachable。
   把 `/etc/apt/sources.list` 里的 `ports.ubuntu.com/ubuntu-ports` 换成
   `mirrors.tuna.tsinghua.edu.cn/ubuntu-ports`（清华 TUNA，实测 ~5MB/s）。
   ROS 2 源同样走 TUNA：`mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu`。
   **只改 ubuntu 源，别动 `nvidia-l4t-apt-source.list`**（nvidia 走 `repo.download.nvidia.cn`，国内可达）。
   备选镜像：ustc / aliyun / huaweicloud 都可达 443。
2. **强制 IPv4**：`echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4`（板子 IPv6 不通）。

**升级前必做**：`apt-mark hold` 所有 `nvidia-l4t* / nvidia-tegra* / nvidia-jetpack` 包，
避免 apt upgrade 意外改动刚刷进去的 bootloader/内核。内核由 nvidia 源提供，不在 ubuntu 升级列表里，
但 hold 是保险。

**Why:** 不换源 apt 直接超时，整个安装无法进行；不 hold nvidia 包，系统更新可能破坏
刚验证过的刷机结果（见 [[jetson-platform-baseline]]）。

**How to apply:** SSH 上板子跑任何 `apt install` 前，先确认这两项已就位（换过源的 `.bak` 备份存在、
`99force-ipv4` 存在）。全新刷机后要重做。ROS 2 安装完整记录见 `docs/ros2-humble-install.html`。

**踩坑备忘**：① `echo 内容 | sudo -S tee 文件` 会因 stdin 冲突写空文件 → 改用 `/tmp` 中转 + `sudo cp`；
② `pkill listener` 会误杀 sshd（其监听进程名含 `[listener]`）→ 断开 SSH，清 ROS 节点用 `pkill -f demo_nodes_cpp`。
