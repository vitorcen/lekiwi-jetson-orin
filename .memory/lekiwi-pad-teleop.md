---
name: lekiwi-pad-teleop
description: 手柄遥控 = 板载 systemd 守护进程 pad_teleop.py（evdev→ZMQ），与 base_host 一起开机自启
metadata:
  type: project
---

手柄遥控**不走 GUI**（用户 2026-07-18 定的方向）：接收器直接插 Orin，板上
`pad_teleop.py` 用 evdev 读手柄，转成与 GUI 完全相同的 ZMQ 协议
（`{"x.vel","y.vel","theta.vel"}`）推 `tcp://127.0.0.1:5555` 给 base_host。
串口永远只有 base_host 一个所有者；ZMQ PULL 公平排队，GUI 键盘与手柄可共存。

**选型结论**：ZMQ 而非 ROS 2 —— host 只说 ZMQ，ROS 2 得加桥接节点纯增负担
（ROS 2 Tab 留给建图导航）。读手柄用 evdev 而非浏览器 Gamepad API
（Linux WebKitGTK 的 Gamepad API 不可靠，且 daemon 本就无 GUI）。

**开机自启**：`/etc/systemd/system/{base_host,pad_teleop}.service`（User=jatson，
Restart=always）。板端源码在项目 **`board/`**（1:1 镜像板子文件系统：
`board/home/jatson/*.py` + `board/etc/systemd/system/*.service`），用
部署脚本：首次 **`scripts/setup_board.sh <ip>`**（装单元 + 一条范围极小的
`/etc/sudoers.d/lekiwi-deploy` NOPASSWD 规则，仅允许 jatson 免密 restart/stop/start/
daemon-reload 这两个服务，无密码可入库），之后 **`scripts/deploy_board.sh <ip>`** 全程
免密 rsync + `sudo systemctl restart`。SSH 公钥认证本机早已配好（`ssh-copy-id` 已生效）。
板子 sudo 密码只在那**一次** setup 时手动输入，绝不写进任何文件/记忆。
2026-07-18 已装好并 enable，之前手工 setsid 起的 base_host 已废弃改由 systemd 管。
日志：`journalctl -u pad_teleop -u base_host`。

**手柄硬件**：DragonRise `0079:181c` "Controller"（通用 USB 手柄），event9/js0。
轴：左摇杆 ABS_X/ABS_Y，右摇杆 ABS_Z/ABS_RZ，量程 0-255 中位 128，十字键 HAT0X/Y。
按键映射（2026-07-18 定稿）：左摇杆平移（模拟）、右摇杆X旋转（模拟）、
**十字键←→=转向（数字 Q/E，用户点名要的）**、十字键↑↓=前后、LB/RB 调速三档
（同 GUI 0.10/0.25/0.40）、**B=按住才急停（瞬时，无闩锁）**。死区 0.18。
教训：闩锁式急停在杂牌手柄上是死局——丝印≠事件码，用户按"START"解不了锁，
车看着像坏了。急停一律做成 momentary；未映射按键全打日志便于对码。

**串口自愈**：开车时电机负载扰动会让 CH343 USB 掉线重枚举（ttyACM0→ttyACM1，
dmesg tegra-xusb TRB 错误）。对策：service 用 `/dev/serial/by-id/usb-1a86_..._5B61036495-if00`
稳定路径 + base_host 遇串口 OSError 直接 exit(1) 靠 systemd Restart=always 重连。
serial 错误绝不能当"bad command"吞掉（吞了就永远攥着死句柄）。

**血泪坑（2026-07-18，车自己跑了）**：evdev 只在轴*变化*时发事件，启动时拿不到
当前值；若把未收到事件的轴默认成原始 0，在 0-255/中位 128 的手柄上等于**满偏 -1**
→ daemon 一启动就全速开车、没人碰手柄。**必须用 `dev.absinfo(code).value` 把所有
轴初始化成真实当前位置**，且归一化函数对 None/未知轴一律返回 0（回中），
永远不许把"没数据"解释成"有偏转"。

**依赖**：lerobot conda env 里 `pip install evdev`（2026-07-18 已装）。
热插拔：找不到 BTN_GAMEPAD 设备就 2s 轮询等待；读 OSError（拔线）→ 发零速重找。
摇杆回中沿发一条零速，host watchdog 兜底。

**臂控制演进（2026-07-18 定稿）**：手柄左手控臂 = 三姿态模型（休息位/前伸/竖直,
关节空间指数趋近），不是笛卡尔 IK——用户明确要"前推=伸到最前,后拉=回休息位"。
START=收臂+断扭矩(momentary 安全序列 relax/wake)。**2026-07-18 已跑
lerobot-calibrate**(homing offset 写进舵机 EEPROM,原始读数坐标系从此改变,
标定前测的姿态常量全部作废重测过);标定命令在根 README。lekiwi 默认配置带
front/wrist 相机,没插相机必须 `--robot.cameras='{}'` 否则 connect 就崩。
**主臂遥操作**：主臂插 GUI 电脑(又一块 CH343,SN 5B3D041438→本机 ttyACM0),
GUI 面板连接/摆中位/对齐零位/跟随;线协议 `arm.dq`=[6 关节相对主臂零位的差值],
base_host 用 **ARM_MID(标定中位,全2048)**+dq 还原。底盘键改为
"x.vel 在消息里才驱动底盘",主臂流不会干扰键盘/手柄开车。
Rust serialport worker 同铁律:独立线程,不进 Tauri runtime。

**对齐必须在中位,不能在休息位**(2026-07-18 教训):休息位是含糊的折叠姿态,
主臂靠手模仿会差到 90°(4 号腕实测);中位每个关节都是直角/伸直,肉眼能对准。
流程:GUI「从臂摆中位」(`arm.mid` 键,从臂自动立正)→ 主臂摆同样中位 → 对齐。
零位存 `~/.config/lekiwi-console/leader_zero.json`,连接自动加载,可重新对齐覆盖。
停止跟随自动发 `arm.relax` 收臂松弛;GUI 也有「收臂松弛」常驻按钮(=手柄 START)。
GUI 启动自动连 ZMQ + 主臂(主臂缺席静默)。

**通用日志总线（2026-07-18）**：手柄在板上、GUI 在桌面,两者唯一连接是 GUI→板
推指令,GUI 看不到手柄。为「按遥控看是哪个键」加了**单向日志通道**:pad_teleop.py
除 PUSH→5555 外,再 bind 一个 **PUB `tcp://*:5556`**,每次按键/推杆广播一行
`{"src":"pad","text":"BTN_SOUTH ↓ 爪子合"}`;轴做 0.25 步长去抖(HAT=±1/0,
扳机=按下/松开),按键取 press/release 边沿。GUI 端 Rust 一个 **SUB worker**(同铁律:
独立 tokio 线程,`zeromq::SubSocket` + `subscribe("")`)连 `<ip>:5556`,收到就
`app.emit("log", text)`;前端 `log.js` 时间戳+来源标签渲染到底部日志栏。
`log_connect(ip)` 命令随 `zmq_connect` 一起触发(同一板子 IP)。刻意做成**通用**:
线格式是 `{src,text}`,任何板端进程往 5556 丢就能显示;键盘开车/主臂动作也调
`logLine()` 进同栏。用户定的方向「通用通道,后续其他日志按需」。

相关：[[lekiwi-gui-tauri]]（GUI 键盘链路 + ZMQ 线协议详情）、[[lekiwi-robot-target]]。
