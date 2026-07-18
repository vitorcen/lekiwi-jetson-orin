---
name: jetson-platform-baseline
description: 本项目实验平台的硬件与系统基线 — Orin Nano 8GB (SKU 3767-0005) + 256GB NVMe + JetPack 6.2.2
metadata:
  type: project
---

实验平台基线（2026-07-16 刷机确认）：

- **模块**：Jetson Orin Nano 8GB devkit，模块 SKU **3767-0005**（非 0003）。
  刷机时板子自报此 SKU，生效 DTB 为 `tegra234-p3768-0000+p3767-0005-nv-super.dtb`。
- **系统**：JetPack 6.2.2 / Jetson Linux **36.5**（Ubuntu 22.04，内核 5.15，CUDA 12.x）。
- **存储**：256GB M.2 NVMe 承载完整 rootfs（QSPI 只放 bootloader/UEFI），**不使用 SD 卡**。
- **Super 模式**：67 TOPS 需系统内 `nvpmodel -m 2`（MAXN_SUPER）开启，刷机不会自动启用。
  ⏳ **待办（截至 2026-07-16 尚未开启）**：当前仍是默认功耗档，跑性能测试/推理前需先 `sudo nvpmodel -m 2` 并 reboot。
- **外设规划**：移动底盘、SO-101 机械臂、IMU、相机与传感器。

**Why:** 后续实验的算力上限、CUDA/容器选型、可用磁盘空间都由这条基线决定；
SKU 0005 这个细节尤其关键 —— 它决定了哪些 board config 可用（见 [[jetpack-version-choice]]）。

**How to apply:** 选 NGC 容器 / PyTorch wheel / jetson-containers tag 时按 **JetPack 6.x + CUDA 12** 匹配，
不要按 JetPack 7 / CUDA 13。跑推理前先确认 `nvpmodel -q` 处于 MAXN_SUPER，否则性能测试数据不可比。
刷机与踩坑详情见 `docs/orin-nano-super-install.html`。
