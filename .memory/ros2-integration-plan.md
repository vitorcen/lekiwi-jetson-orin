---
name: ros2-integration-plan
description: ROS 2 集成计划已定稿（2026-07-20，经 codex gpt-5.6-sol 评审）— 并行层接入、安全门下沉 base_host、slam_toolbox+AMCL+DWB 先行；动 ROS 前必读
metadata:
  type: project
---

2026-07-20 定稿 ROS 2 集成计划，全文 `docs/ros2-integration-plan.html`（含架构 SVG、
P0-P7 阶段验收门、砍掉/推迟清单、codex 评审摘要）。背景：USB 深度相机已接
（型号待上电确认，当天板子不在线），单线激光雷达在途（型号未定）。

核心决策（改 ROS 相关代码前先读原文）：
- **并行层，不替换**：base_host 仍是串口唯一属主 + 唯一仲裁器；ROS 经
  `lekiwi_base_bridge`(/cmd_vel→ZMQ 5555) 作为第 4 控制源，优先级 `pad>gui>ros>mcp`。
- **安全门在 base_host 仲裁后**（对所有源生效），ROS 侧 lekiwi_safety 只出净空裁决
  （PUB :5558 带有效期）；ROS 侧不做第二套 mux/watchdog。
- 命令协议升级 src+seq+单调 ts+TTL + latest-only 排空；控制租约与零速流分离。
- odom：base_host 只批量读轮 7/8/9 → PUB :5557（带板端单调时钟）→ ROS 侧软件展开
  int64 计数（STS3215 多圈掉电不存）算正运动学。无 IMU 不上 EKF。
- 栈选型：slam_toolbox 建图 + AMCL 定位 + NavFn/DWB（配 vel_y 用横移）→ MPPI 后验 A/B。
  砍掉：Cartographer、TEB、nvblox/Isaac ROS。深度相机一期只预览+rosbag 不进 costmap。
- P2（里程计+横移标定）是杀关卡：轮反馈 ≥30Hz、p95<40ms 不达标就停，不准进 SLAM。
- rosbag 与 lerobot 数据集两条线独立，共享 run_uuid/时间基准，不统一格式。
- 仓库布局：`ros2/` 五包（base_bridge/description/safety/recorder/bringup），
  nav2 参数 yaml 必须进 repo（yahboom 的教训：参数只在板上没入仓）。

参考项目 `/Users/david/work_ai/yahboom-rdk-x5/` 的可搬资产与踩坑见其 `.memory/`
（cmd_vel_mux、safety_stop、episode_recorder、strafe 标定方法论最有价值）。
相关：[[ros2-humble-installed]]、[[lekiwi-robot-target]]、[[lekiwi-gui-tauri]]、[[drive-mcp-skill]]。
