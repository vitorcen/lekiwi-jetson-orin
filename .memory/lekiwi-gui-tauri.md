---
name: lekiwi-gui-tauri
description: LeKiwi 控制台在 gui/ (Tauri)，ZeroMQ Tab 键盘遥控已成，ZMQ 必须在 Rust 后端不能在前端
metadata:
  type: project
---

桌面 GUI 在 `gui/`（2026-07-18 建），照 `../RDK-experience/gui` 的 Tauri 结构：
Rust 后端 + vanilla JS 前端（`ui/js/` 按功能分模块，`state.js` 出 `$/S/invoke`，
`main.js` 切 tab）。`./run.sh` 启动（debug），`--release` 优化版。编译实测通过
（Tauri 2.11 + 纯 Rust zeromq 0.4 crate，零 warning）。

**Tab 设计**（用户要求「醒目」）：大号图标 + 中英文字 + 选中态蓝色高亮条。
- 🔌 **ZeroMQ 遥控** = 已实现，键盘 WASD 或**方向键**平移 / QE 旋转 / RF 调速 / 空格急停。
  **点遥控面板任意处**（左键盘区或右运动状态区）即激活键盘（`#telewrap` 为 focus 目标，
  加 `.armed` 高亮两个 panel）；失焦/切 Tab/隐藏自动停车。
- 🤖 **ROS 2** = 占位，后续做建图导航等。

**核心架构铁律 1：ZMQ 必须在 Rust 后端，不能在前端。**
WebView 开不了 ZMQ socket（ZMQ 不是 WebSocket）。所以 `src-tauri/src/main.rs` 持有
一个 PUSH socket，前端通过 Tauri `invoke` 调 `zmq_connect / zmq_send_base /
zmq_disconnect / zmq_status`。（对比 RDK 那个 GUI 前端直连 rosbridge websocket——
那是因为 ROS 有 websocket 桥，ZMQ 没有。）

**核心架构铁律 2（血泪坑，2026-07-18）：纯 Rust `zeromq` crate 的 socket 不能直接
在 Tauri 的 async command / runtime 里用。** 现象：`connect()` 返回 Ok，但**从不建立
TCP**（板子 `ss` 看不到连接），于是每个 `send` 200ms 超时 → 前端"发送失败"。
根因：zeromq crate 每个 socket 有个后台 IO 任务，需要一个**持续存活的 tokio runtime**
驱动；Tauri 自己的 async runtime 驱动不了它。
**解法**：起一个**独立 std::thread + 自建 `tokio::runtime::Runtime`**（= 独立二进制的
环境，已用最小 repro 验证 `ss` 显示 ESTAB），socket 全程活在这个 worker 里；Tauri
command 只通过 `mpsc`/`oneshot` channel 和 worker 通信（command 里不碰 zmq）。
诊断方法留档：写个最小 Rust 程序用同款 zeromq crate 连板子——独立 runtime 里能连、
Tauri runtime 里不能连，一测就分明。

**另一个环境坑**：在 agent 的 Bash 里**启动不了 Tauri GUI**（图形程序，exit 144 吞输出，
sandbox 开关都无效）——只能让用户在自己桌面 `./run.sh` 跑。所以 GUI 的端到端验证靠
「板子侧 `ss`/`base_host.log` 观察 + 最小 repro」间接完成，不是直接点 GUI。

**ZMQ 线协议**（对齐 lerobot 0.5.2 `lekiwi_host`/`lekiwi.py`，见 [[lerobot-installed-orin]]）：
host 在 `tcp://*:5555` bind PULL socket，每条命令一段 JSON：
`{"x.vel": <m/s>, "y.vel": <m/s>, "theta.vel": <deg/s>}`。host 过滤 `.vel` 结尾的 key
丢进 `_body_to_wheel_raw`，**三个 key 必须都在**（host 直接索引）。观测/视频走 5556
（host PUSH，JPEG→base64，本 GUI 暂未用）。

**遥控安全（必须保留）**：host 有 idle watchdog，命令断流即停车。所以前端 **20Hz 持续
流式发**，松开所有键立刻补一条零速；切 Tab / 窗口失焦 / 页面隐藏都发零速（dead-man，
`onLeaveZmq` + `blur` + `visibilitychange`）。Rust send 加 200ms timeout，防 host 没起时
PUSH 卡死前端。速度三档严格对齐 lerobot：`0.1/0.25/0.4 m/s`、`30/60/90 °/s`。

**用前提**：Orin 上先起 host（车通电、9 电机在线，见 [[lekiwi-robot-target]]、
[[servo-bus-ch341-bringup]]），GUI 填 IP+5555 连接。说明见 `gui/README.md`。

**坑 — 原版 `lekiwi_host` 起不来（SSH 下）**：它 `connect()` 强制交互式标定
（`input("Move robot to the middle...")`），SSH 非交互 stdin=EOF → **EOFError 崩**。
即只想开底盘也被逼标定整臂。要用原版：先在能交互的终端跑
`lerobot-calibrate --robot.type=lekiwi --robot.port=/dev/ttyACM0 --robot.id=orin_kiwi`
（手摆姿势），之后 host 才起得来。

**解法 — 免标定底盘 host `board/home/jatson/base_host.py`**（2026-07-18）：轮子本就不用
标定（官方明说），所以写了个只驱动 7/8/9 的 ZMQ host，**同样的线协议**（PULL 5555 收
`{"x.vel","y.vel","theta.vel"}`），GUI 一行不用改。运动学复用 `base_move.py`（与 lerobot
零误差）。现由 systemd 管（`scripts/deploy_board.sh` 部署重启，手工 start/stop 脚本已废弃）。
用 conda lerobot env 的 python（有 pyzmq 27.1）。带 watchdog（0.5s 断流停车）。
已实测：bind 5555、收零速命令解析正常、进程稳定。**唯一未端到端实测**：Rust 纯 zeromq
crate ↔ pyzmq 互通（ZMTP 标准应通），要 GUI 连上架空开车才最终确认。

**注意**：base_host 与原版 lekiwi_host 都 bind 5555，**二选一起**，别同时跑。
base_host 只管底盘，不涉及臂/录数据；要完整 lerobot 流程仍走原版+标定。
