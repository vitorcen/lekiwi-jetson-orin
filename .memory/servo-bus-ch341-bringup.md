---
name: servo-bus-ch341-bringup
description: SO-101 舵机总线 bring-up — CH340 驱动板需自编 ch341.ko + 卸 brltty；舵机是 Feetech STS3215，ID1@1Mbps
metadata:
  type: project
---

SO-101 机械臂舵机总线 bring-up（2026-07-16，板子 192.168.3.189）：

**硬件链路**：USB 转 TTL 驱动板 → Feetech 总线 → 舵机。舵机型号 **Feetech STS3215**
（SCS/STS 协议），波特率恒为 **1000000**，供电 12V 级。

**手上有两种驱动板，认准 USB ID**：
- **CH343**（`1a86:55d3`，"USB Single Serial"）= **LeKiwi 随机的总线板**，走内核原生
  CDC-ACM → `/dev/ttyACM0`，**免驱、无需 ch341.ko、不受 brltty 影响**。日常用这块。
- **CH340**（`1a86:7523`）= 早期单舵机调试用的那块 → `/dev/ttyUSB0`，才需要下面的坑 1/坑 2。

2026-07-16 实测（CH343 板）：**9 个电机全部挂同一条总线、一个 USB 口**，ID **1~9** 齐全无冲突。
从臂 1~6 出厂已编号（无需 `lerobot-setup-motors`）；轮子 7/8/9 是我们用 `~/set_servo_id.py`
逐个写的。lerobot 自己的 `FeetechMotorsBus` 也读通全部 9 个（见 [[lerobot-installed-orin]]）。

**改 ID 的铁律**：ID 存在 EEPROM 5 号寄存器，**舵机没有 SN，ID 就是它唯一的身份**。
新舵机出厂全是 ID 1 → **一次只能接一个**改号，否则两个一起应答、总线打架。
改法：解锁 55 号写 0 → 写 5 号新 ID → **用新 ID** 写 55 号 = 1 重新上锁（漏了 EEPROM 一直开着）。
`~/set_servo_id.py <port> <旧> <新>` 已封装，并强制"总线上只有旧 ID 一个"才执行。

**轮子的 ID ↔ 位置对应关系是 lerobot 写死的**（v0.5.2 源码 `robots/lekiwi/lekiwi.py`）：
**7 = `base_left_wheel`(150°)、8 = `base_back_wheel`(-90°)、9 = `base_right_wheel`(30°)**，
`base_radius=0.125m, wheel_radius=0.05m`，`raw = deg/s × 4096/360`，超速时三轮**按比例同缩**。
判据：**前进时后轮(8)速度恒为 0**（cos(-90°)=0），左右轮反向对推；横移时后轮出力最大。

**轮子驱动（实测确认）**：位置模式转不了连续圈 → 切**速度模式**：**33 号寄存器写 1**
（EEPROM，需解锁），速度写 **46/47 号寄存器**，**bit15 = 反向**（`raw | 0x8000`）。
lerobot 连接时也会把 base 电机设成 VELOCITY，不冲突。`~/base_move.py` 已实现全向运动学，
与 lerobot 的 `_body_to_wheel_raw` 交叉验证**零误差**。

机器人整体拓扑见 [[lekiwi-robot-target]]。

**坑 1 — L4T 内核缺 CH341 驱动**：`CONFIG_USB_SERIAL_CH341 is not set`，插上认得到
USB 芯片但生不成 `/dev/ttyUSB0`（内核只带 cp210x/ftdi_sio，没 ch341）。
解法：out-of-tree 自编。板子已装 `nvidia-l4t-kernel-headers`（`/lib/modules/$(uname -r)/build`
符号链接可解析，有 `.config`+`Module.symvers`），gcc/make 齐全：
1. 抓 mainline `drivers/usb/serial/ch341.c`（v5.15.185，`git.kernel.org` 可达，纯自包含）。
2. Makefile：`obj-m += ch341.o` + `make -C $(KDIR) M=$(PWD) modules`，板子原生编（aarch64）。
3. 装持久：`cp ch341.ko /lib/modules/$(uname -r)/kernel/drivers/usb/serial/ && depmod -a`
   → 热插拔自动加载。源码+ko 存在板子 `~/ch341-build/`。
未签名会 `taint kernel`（无害）。**内核被 nvidia 升级后要重编**（`apt-mark hold` 已挡，见 [[jetson-apt-network-cn]]）。

**坑 2 — brltty 抢设备**：ch341 挂上 `ttyUSB0` 后一闪即被 `brltty`（盲文点显器守护进程，
认同款 `1a86:7523`）抢走踢掉（dmesg: `interface 0 claimed by ch341 while 'brltty' sets config`）。
Ubuntu 22.04 CH340 经典坑。解法：`apt remove brltty`（机器人板子无盲文设备，安全）。

**坑 3 — 串口权限**：`jatson` 原不在 `dialout` 组 → `usermod -aG dialout jatson`（需重新登录生效）。

**How to apply:** 换舵机 SDK 时优先 Feetech `scservo_sdk` 或 lerobot，波特率 1000000。
裸协议扫描/控制脚本在板子 `~/scan_servos.py`（ping ID + 读位置/电压/温度）、
`~/move_servo.py`（扭矩使能→目标位置→回读，STS 内存表：Torque_Enable=40, Acceleration=41,
Goal_Position=42 LE, Goal_Speed=46 LE, Present_Position=56, Voltage=62, Temp=63；
INST PING=0x01 READ=0x02 WRITE=0x03，校验和 `~(sum(ID..params))&0xFF`）。
板子 apt 装包前提见 [[jetson-apt-network-cn]]，ROS 集成见 [[ros2-humble-installed]]，平台基线 [[jetson-platform-baseline]]。
