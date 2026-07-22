---
name: imu-closed-loop-drive-plan
description: IMU 闭环控车方案已定稿(2026-07-22,Claude+codex 双模型)— P0 先修 base_host 命令链,闭环放独立 ROS2 controller;动控车代码前必读
metadata:
  type: project
---

2026-07-22 定稿「结合 IMU 让 LLM 控车更准」方案,全文 `docs/imu-closed-loop-drive.html`
(与 codex gpt-5.6-sol 头脑风暴,关键代码断言逐条核实)。

核心决策:
- **LLM 交目标不拼速度帧**:turn_by(angle_deg)/motion_stop/motion_status 为 MVP 工具面;
  轮 odom 后加 move_by(dx,dy)/turn_to(REP-103 yaw,不叫 heading——imu_read 的罗盘 heading
  与底盘 CCW theta 符号相反,两套并喂必出符号事故)/受限 execute_motion(≤8 步,失败即停,
  绝不自动续跑)。drive_move 保留降格为低层调试接口。
- **闭环放独立 ROS2 motion_controller**(直订 /imu/data),不塞 MCP(rosbridge 反馈延迟+
  会话耦合)、不进 base_host。MCP 变薄目标适配器。
- **P0 前置(不是顺手优化),base_host 实锤问题已核实**:①drive() 逐轮 txrx,每轮
  4ms sleep+read(64) 等满 20ms 超时,3 轮≈72ms>20Hz 周期 50ms;②队列逐帧执行陈旧命令
  (停车帧排队);③MCP frames_sent 只是进了 ZMQ≠仲裁采用;④vx/vy 分量各钳 0.15,对角
  模长 0.212。修法:三轮 broadcast sync-write、每源 latest-only+seq/ts/TTL、发布
  owner/applied_seq、最终钳位(向量模长)下沉 base_host——与 [[ros2-integration-plan]]
  的协议升级是同一件事。
- **纯 IMU 只能闭环 yaw**(±2–3° 可期对外说 ±5°;P 控制不加积分项)+平移航向保持
  (P1.5,距离仍 v×t 须标 distance_estimated=true)。加速度二次积分不做(0.01g 偏置
  10s 漂 4.9m)。磁力计标定 MVP 不做(舵机动态磁扰非八字校准可修)。
- **P2 轮 odom 直接 sync-read ≥30Hz**,不做 5Hz 逐轮读;串口瓶颈是事务模型非 1Mbps 带宽。
- 安全新增:闭环会坚持错误方向——IMU 年龄>120–200ms/epoch 变/无进展/振荡 全部立停,
  禁止"IMU 坏了退化按时间跑" fallback;pad 接管→preempted_by_human 不偷偷续跑;
  雷达 gate 在 base_host 仲裁后全源生效,>0.3m 包络任务必须先有 gate。
- 标定顺序:时序坐标系→gyro 量程(±2000dps 假设未验)→yaw 电磁干扰(分舵机状态录)→
  转向实测→轮几何;磁力计最后且 MVP 跳过。

**实施进展 2026-07-22(P0+P1 代码落地,板上软件链路全验)**:
- base_host:sync-write 三轮一包(`sync_write_pkt`,~72ms→<1ms/帧)、latest-only 排空
  (`pending` 只应用最高优先级最新帧)、TTL(`frame_ttl_ok`,未盖戳旧帧永远放行)、
  仲裁后 `clamp_body` 向量模长钳位(BODY_VMAX 0.35/BODY_WMAX 90)、反馈 PUB **:5557**
  (每应用帧 ack {owner,seq,vx,vy,om} + 2Hz state {motion_on,owner,moving};
  :5556 被 pad_teleop 占用,:5557 正好是计划里 base_host 反馈通道,将来轮 odom 同 socket)。
  BASE_PRIO 加 "ros"=2(pad>gui>ros>mcp)。
- motion_controller(`~/ros2/motion_controller.py` + user 服务,系统 python3 需
  `pip3 install --user pyzmq`):纯逻辑 YawTurn 状态机(P 控制无积分,KP0.8/ω∈[6,25]、
  容差 2°+静止 4 帧、no_progress 1.5s/sensor_jump 45°/振荡 8 翻转/超时全显式终止),
  REP :5560 收 turn_by/stop/status,SUB :5557 acks 检测 preempted_by_human/not_applied,
  从 state 心跳读 motion_on 把「安全开关关着」写进失败原因。
  **坑:类方法不能叫 `handle`——覆盖 rclpy.Node.handle property,Node.__init__
  `with self.handle:` 直接 AttributeError: __enter__**。
- MCP:turn_by(±180 钳位,轮询到终态)/motion_status;drive_move 帧盖 seq/ts/ttl 戳,
  数 ack 报 `frames_applied`/`wheels_driven`,应用数 0 时按 motion_on 给准确警告——
  「frames_sent>0 = 成功」的谎报已灭。drive_move 描述降格为开环低层/降级接口。
- 真机验证(motion 开关关闭状态):turn_by → not_applied 0.65s 诚实终止;语音端到端
  LLM 播报「安全开关关闭,帮我打开」——修复前它谎称「转了90度完成!」。
  **待办:打开运动开关后做 ±30/±90/±180° 实测精度(P1 验收门 ≤3°)**。
相关:[[drive-mcp-skill]]、[[ros2-integration-plan]]、[[lekiwi-pad-teleop]]。
