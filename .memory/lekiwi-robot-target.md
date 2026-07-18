---
name: lekiwi-robot-target
description: 目标机器人是维特智能 LeKiwi — 3轮全向底盘 + SO-101 从臂，9 电机单总线 ID1~9；先做手柄遥操作
metadata:
  type: project
---

本项目的目标机器人（2026-07-16 确认）是**维特智能（WitMotion）版 LeKiwi**：
3 轮全向移动底盘（3× STS3215-C018，总线 ID **7/8/9**）+ SO-101 从臂
（6× STS3215-C018，ID **1~6**，1=底座关节 6=夹爪），**9 个电机共用一块舵机
驱动板、一条 USB 总线**（臂 1 号舵机第二接线口下穿插到底盘驱动板的第二串口座）。
Leader 主臂是 7.4V 舵机（C044/C001/C046），单独驱动板——当前方案用**手柄替代
主臂**做遥操作，Leader 暂不接。供电：11.1V 5600mAh 电池 → DC 分线器 →
舵机板 12V 直供；Orin devkit 收 9~20V DC，可由分线器直供，**教程里的
12V→5V 降压模块是给树莓派的，Orin 不用**。

**软件路线**：lerobot（不走 ROS），锚定教程验证过的 commit
`26ff40ddd784280efc133a8e5af1a76e5ac731c2`，`pip install -e ".[lekiwi]"`，
host 与 teleop 客户端都跑在 Orin 上（`remote_ip=127.0.0.1`）。
实施方案全文见 `docs/lekiwi-orin-teleop-plan.html`。

**资料来源**：维特智能语雀 4 篇（lekiwi介绍/组装、RDK X5、树莓派部署），
知识库 `wit-motion.yuque.com/wumwnr/wf4p82`。页面 JS 渲染，WebFetch 只拿到标题；
**正文可匿名走 API**：`GET https://wit-motion.yuque.com/api/docs/<slug>?book_id=76478325&mode=markdown`
（加 UA + `x-requested-with: XMLHttpRequest` 头），`data.sourcecode` 即 markdown。

舵机总线/驱动的坑见 [[servo-bus-ch341-bringup]]，平台基线 [[jetson-platform-baseline]]，
装包网络前提 [[jetson-apt-network-cn]]。
