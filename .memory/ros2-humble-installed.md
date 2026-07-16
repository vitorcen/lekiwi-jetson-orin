---
name: ros2-humble-installed
description: 板子已装 ROS 2 Humble desktop 0.10.0，环境已配 SSH 免密，pub/sub 验证通过
metadata:
  type: project
---

Orin 板子（192.168.3.188）**已安装 ROS 2 Humble desktop**（2026-07-16）：

- 版本：`ros-humble-desktop` 0.10.0，RMW = `rmw_fastrtps_cpp`（默认 Fast DDS）。
- 装在 `/opt/ros/humble`，`~/.bashrc` 已 source，`ROS_DOMAIN_ID=0`。
- 工具链：`ros-dev-tools`、`python3-colcon-common-extensions`、`rosdep`（已 init + update）。
- 视觉：`libopencv-dev` 来自 nvidia 源（r36.5），带 CUDA 加速。
- 验证：talker/listener pub/sub 通、`ros2 node/topic` 内省正常。
- **SSH 免密已配**：本机（vitor-desktop）公钥已注入板子 `~/.ssh/authorized_keys`，
  直接 `ssh jatson@192.168.3.188` 免密登录。板子用户 `jatson`。

**How to apply:** 后续要在板子上跑 ROS，直接 SSH 上去即可（环境已自动加载）。
新建工作区：`~/ros2_ws/src` + `colcon build`。SO-101 机械臂/相机/IMU 驱动用
`rosdep install --from-paths src -y` 解依赖。装新 apt 包前先看 [[jetson-apt-network-cn]]（换源前提）。
平台基线见 [[jetson-platform-baseline]]。
