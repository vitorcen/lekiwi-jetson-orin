---
name: drive-mcp-skill
description: 受限控车 MCP(drive/mcp_server.py)已挂载 Hermes — 钳位 0.15m/s·2s·0.3s 冷却,无人值守仍 gate 在 base_host v2
metadata:
  type: project
---

2026-07-19 控车 skill 上线并挂载(robot profile mcp_servers.drive,复用 vlm/.venv+pyzmq)。
六工具:drive_move(vx,vy,omega,duration)/drive_stop/drive_status/imu_read/
turn_by(IMU 闭环转向,±180°)/motion_status。turn_by 走 motion_controller REP :5560,
drive_move 已按 base_host :5557 ack 报 frames_applied 真值(见 [[imu-closed-loop-drive-plan]])。

**imu_read(2026-07-22)**:只读 10 维 IMU 快照——经 rosbridge ws :9090 一次性订阅
`/imu/data|mag|temp|pressure` 各收一条(≤3s,气压 2Hz 限速故超时须 >0.5s),返回
roll/pitch/yaw+罗盘 heading(由融合四元数,0°=上电参考方向 **非绝对地磁北**;
GUI 同款换算 heading=(-yaw)%360)、陀螺 dps、加速度、磁力计原始计数(量纲未标定,
结果里带中文 note 提醒 LLM)、温度(传感器内部温,偏高)、气压 hPa、ISA 海拔。
串口唯一属主是 imu_10dof 节点,rosbridge 是唯一合规读路径。纯计算函数
(imu_euler_deg/imu_heading_deg/isa_altitude_m/imu_payload)有单测
tests/test_drive_imu.py(importlib 显式路径加载,因 vlm/mcp_server.py 同名遮蔽)。
真机验证:直连快照全字段 + 语音端到端(问朝向/气压→Agent 调 imu_read 正确播报)。真机验证:前进退回、
钳位(1.0→0.15 并注明)、冷却拒绝、busy 不排队、stop 抢断(40 帧只发 1 帧)、语音端到端
("往前走一小步再退回"→Hermes 自主 status+两次 move+逐步播报)。

2026-07-19 起 MCP 帧带 `"src":"mcp"`,在 base_host 底盘 mux 里排最低——手柄/GUI
活动的 0.5s HOLD 窗口内 MCP 底盘帧直接丢弃(见 [[lekiwi-pad-teleop]])。

安全分层:本文件是第 2 层 schema 硬钳位(|v|≤0.15m/s、|ω|≤30°/s、0.1-2.0s、20Hz 重发
+3 帧零刹停、互斥不排队、0.3s 冷却);第 3 层靠 base_host 0.5s watchdog 兜底(MCP 挂了
车自停)。**已知弱点**:ZMQ PUSH 缓冲(SNDHWM=10),≤0.5s 短移动在 base_host 不在时
误报已发送;真正的到达确认与仲裁(Unix socket 特权通道、人工恒压制 LLM、分域 watchdog)
等 base_host v2(S0)——**无人值守运行前必须完成**,现在是有人监督档。

人格文件在 `~/.hermes/profiles/robot/SOUL.md`(注入系统提示):龙虾=LeKiwi 小车,
身体配置+语音风格(首句短、口语化、无 markdown)+视觉不可信观测+运动守则(含糊先确认、
"停"即 drive_stop、视觉从不触发运动)。原版备份 SOUL.md.bak。
相关:[[hermes-voice-agent-plan]]、[[voice-frontend-s2]]。
