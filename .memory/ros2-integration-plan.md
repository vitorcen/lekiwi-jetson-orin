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

**进展 2026-07-20(P0 部分 + 深度预览抢跑)**:板子 DHCP 换 IP → **192.168.3.189**
(全仓引用已更新;免密公钥重注入)。深度相机认定 = **Orbbec Astra Pro**(2bc5:0403 深度
OpenNI 私有协议 + 0501 UVC 彩色)——与 yahboom 同款,其教训直接继承:厂商 astra_camera
ROS 驱动激光/LDP 坏(深度恒 0)→ **OpenNI2 直读**(redist 取自 orbbec/ros2_astra_camera
仓库 arm64,放板上 `~/openni2_redist`,pip primesense);UVC 彩色与深度 USB2 带宽互掐,
彩色不开。板端已上线(user 服务开机自启):`rosbridge`(:9090,apt
ros-humble-rosbridge-suite)+ `depth-preview`(`~/ros2/depth_preview.py`,320×240 伪彩
JPEG → `/depth_preview/compressed`,实测 15.1fps,停帧看门狗 os._exit 由 systemd 拉起);
udev `56-orbbec-usb.rules`(2bc5 MODE 0666)。Mac 经 rosbridge ws 订阅收帧验证通过。
注意 lsusb -t:板上所有 USB 设备(双 UVC + Astra + 双串口)挤同一条 480M USB2 根。

**进展 2026-07-20 下午(P3 感知抢跑,全部实测通过)**:雷达到货认定 = **LDROBOT LD19**
(D300 系,47 字节包头 54 2C @230400,CRC8 poly 0x4D)。板端 `~/ros2/` 三个新节点 +
user 服务:`ld19_lidar.py`(串口直解 → /scan 10Hz 450 bins,CW→CCW 转 REP-103;
**串口必须走 /dev/serial/by-id/**——雷达和舵机总线是同款 1a86 CH9102 芯片,裸 ttyACM*
序号会漂)、`front_cam.py`(vlm-daemon /frame.jpg HTTP 转发 → /front_cam/compressed
~10fps,vlm 保持单属主;**有订阅者才拉流**,GUI 不看时 vlm 采集自动歇)、`wrist_cam.py`
(Sunplus 1bcf:2281「2M」独立 UVC = 腕部相机,**有订阅才开设备**省 USB2 带宽,释放/
热拔自愈)。踩坑存档:seq 哨兵值 init -1 + seq=0 首 tick 就 encode None 崩——初值
对齐 last_pub_seq==seq 消除该特殊态(depth_preview 同模式但被 last_mono 检查偶然掩护)。

**进展 2026-07-20 晚(10 维 IMU 上线)**:新接 IMU = CH340 串口(1a86:7523,
by-id `usb-1a86_USB_Serial-if00-port0`,**无唯一序列号**——板上仅此一颗 CH340 才稳,
舵机/雷达是 CH9102 不冲突)。协议自逆向:115200,帧 `7e 23 <总长> <type> <payload>
<sum&0xFF>`,四型循环 ~25Hz:04=9轴 int16(accel /2048g 重力已验、gyro **量程假设
±2000dps 未验**(静止全零,喂 EKF 前须实测)、mag 原始计数 scale 未知)、16=四元数
wxyz float、26=欧拉 rpy 弧度(与四元数互算吻合,节点不转发)、32=高度/温度/气压/气压2
float。板端 `ros2/imu_10dof.py` + `imu-10dof.service` → `/imu/data|mag|temp|pressure`
四个标准 topic(mag 是原始计数非 Tesla,已注释)。GUI ROS2 页雷达行右侧 IMU 仪表盘
(人工地平仪+罗盘卡+陀螺/加速度棒图+磁/温/压/高度,euler 由 GUI 从四元数算)。
Mac 经 rosbridge 四 topic 实收验证通过。计划里「无 IMU 不上 EKF」的前提已变——
odom EKF 可提上日程,但先补 gyro 量程实测。

参考项目 `/Users/david/work_ai/yahboom-rdk-x5/` 的可搬资产与踩坑见其 `.memory/`
（cmd_vel_mux、safety_stop、episode_recorder、strafe 标定方法论最有价值）。
相关：[[ros2-humble-installed]]、[[lekiwi-robot-target]]、[[lekiwi-gui-tauri]]、[[drive-mcp-skill]]。
