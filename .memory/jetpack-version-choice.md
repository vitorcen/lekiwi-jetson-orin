---
name: jetpack-version-choice
description: 为何选 JetPack 6.2.2 而非最新的 7.2 — 生态成熟度优先于版本新
metadata:
  type: feedback
---

2026-07-16 刷机时，JetPack **7.2**（L4T 39.2 / Ubuntu 24.04 / CUDA 13.2）已发布，
但用户明确选择 **6.2.2**（L4T 36.5 / Ubuntu 22.04 / CUDA 12.x）。

**Why:** 生态成熟度 > 版本新。NGC 容器、DeepStream、ROS 2 Humble、jetson-containers
仍以 JetPack 6.x 为主力目标；第三方轮子对 CUDA 13 + Ubuntu 24.04 的适配还在追赶，
选 7.2 很可能要自行编译依赖，把时间花在环境上而非实验上。

**How to apply:** 该项目涉及版本选型时（JetPack、CUDA、PyTorch、容器 tag），
默认倾向**有现成轮子/容器的稳定版**，而非最新版；除非用户明确要求尝鲜或有非新版不可的特性需求。
升级 JetPack 前先确认目标生态（如 GR00T / VLA 相关依赖）是否已支持。

相关：[[jetson-platform-baseline]]
