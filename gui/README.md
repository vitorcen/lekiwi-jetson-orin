# LeKiwi 控制台 (Tauri GUI)

桌面 GUI，控制装了 lerobot 的 LeKiwi 车（Orin 上的 `lekiwi_host`）。
参照 `RDK-experience/gui` 的 Tauri 结构：Rust 后端 + vanilla JS 前端。

## 跑起来

```bash
./run.sh            # debug（编译快）
./run.sh --release  # 优化版
```

首次编译会拉 Tauri + zeromq 依赖，较慢；之后只在 `src-tauri/` 或 `ui/` 改动时重编。

## Tab

| Tab | 状态 | 做什么 |
|---|---|---|
| 🔌 **ZeroMQ 遥控** | ✅ 已实现 | 键盘 WASD/QE 开车，走原生 lerobot ZMQ 通道 |
| 🤖 **ROS 2** | 感知预览 | 前端直连 rosbridge(:9090,只读订阅):雷达 /scan 极坐标图、深度相机 JPEG 预览、前视相机预留框;控制仍走 ZeroMQ。整体计划见 `docs/ros2-integration-plan.html` |

## ZeroMQ Tab 怎么用

1. **先在 Orin 上起 host**（车要通电、9 电机在线）。有两个选择：

   **A. 底盘专用 host（推荐，免标定）** — 只驱动 3 个轮子，立刻能开车。
   已做成 **systemd 开机自启**，源码在项目 `board/`（1:1 镜像板子文件系统）。
   首次一次性安装（装单元 + NOPASSWD 规则，仅这次要板子 sudo）：
   ```bash
   scripts/setup_board.sh <board-ip>    # 只跑一次
   ```
   之后改完代码，一条命令部署 + 重启，**全程免密**：
   ```bash
   scripts/deploy_board.sh <board-ip>   # rsync board/ 到板 + 重启服务
   ```

   **B. 原版 lerobot host（要先标定机械臂）** — 底盘+臂都能用，但 `connect()` 会强制
   交互式标定（手摆姿势按回车），必须在能交互的终端里先跑一次
   `lerobot-calibrate --robot.type=lekiwi --robot.port=/dev/ttyACM0 --robot.id=orin_kiwi`，
   之后才能 `python -m lerobot.robots.lekiwi.lekiwi_host ...`。**SSH 非交互下直接跑 host 会 EOFError。**

2. GUI 里填 Orin IP（板子局域网地址）和命令端口（默认 `5555`），点**连接**。
   免手填：`cp gui/config.example.json ~/.config/lekiwi-console/config.json` 后改成实际 IP，
   GUI 启动自动带入（release 版同样读这个路径，可随时手改）。
   每字段优先级：GUI 里输入过的值（localStorage `lekiwi.conn`）> config.json > 空。
3. **点键盘区**获取焦点（虚线框变实线高亮），然后：

   | 键 | 动作 | | 键 | 动作 |
   |---|---|---|---|---|
   | W / S | 前进 / 后退 | | Q / E | 左转 / 右转 |
   | A / D | 左移 / 右移（平移） | | R / F | 升 / 降速档 |
   | 空格 | 急停 | | | |

4. 松手即停；切走 Tab、窗口失焦、页面隐藏都会自动停车（dead-man）。

**先把底盘架空再试。**

## 手柄遥控（板载守护进程，无需 GUI）

手柄接收器直接插 **Orin**，板上 `pad_teleop.py`（evdev 读手柄）把摇杆转成同一条
ZMQ 协议推给本机 `base_host`，**开机自启**，和 GUI 键盘可共存（PULL 公平排队多个
PUSH 端）。串口始终只有 base_host 一个所有者。

```bash
# 首次：scripts/setup_board.sh（装单元 + 免密规则，要 sudo 一次；板上先 pip install evdev）
# 日常：改完免密部署
scripts/deploy_board.sh <board-ip>
# 看日志
ssh jatson@<board-ip> 'journalctl -u pad_teleop -u base_host -n 30 --no-pager'
```

板端源码目录布局（`board/` 镜像板子根，`scripts/deploy_board.sh` 用 rsync 同步）：

```
board/home/jatson/base_host.py      → /home/jatson/base_host.py
board/home/jatson/pad_teleop.py     → /home/jatson/pad_teleop.py
board/home/jatson/probe_bus.py      → /home/jatson/probe_bus.py   （总线诊断）
board/etc/systemd/system/*.service  → /etc/systemd/system/
```

| 手柄输入 | 动作 | 对应键盘 |
|---|---|---|
| 左摇杆 | 平移：前后 + 左右横移（模拟量） | W/S + A/D |
| 右摇杆左右 | 转向（模拟量，细调用它） | Q/E 无级版 |
| 十字键 ←/→ | 左转 / 右转（数字量满速档） | Q/E |
| 十字键 ↑/↓ | 前进 / 后退（数字量） | W/S |
| LB / RB | 降 / 升速档（0.10/0.25/0.40 m/s） | F/R |
| B **按住** | 瞬时急停（松开即恢复，无闩锁） | 空格 |

摇杆回中即发零速停车；手柄拔掉发零速并等待热插拔；base_host 的 watchdog 仍是兜底。
杂牌手柄丝印≠事件码：按未映射键会在 `journalctl -u pad_teleop` 里打出真实键码，照此改映射。
**也可直接看 GUI 底部日志**：`pad_teleop.py` 每次按键/推杆都往 `tcp://*:5556`（PUB）
广播一行 `{"src","text"}`，GUI 底部日志栏实时显示「BTN_SOUTH ↓ 爪子合 / ABS_Y +0.72 臂前伸」，
按一下就知道是哪个键——对码不用再 ssh 看 journal。键盘开车、主臂操作也进同一栏。

## 主臂遥操作（Leader arm，GUI 内）

主臂(SO-101 leader)USB 插**跑 GUI 的电脑**，ZeroMQ Tab 中间「主臂遥操作」面板操作，
关节空间直通：`从臂目标 = 从臂休息位 + (主臂当前 − 主臂零位)`，30Hz 走同一条 ZMQ
通道（`arm.dq` 键），base_host 端行程限位 + 单步限幅兜底。手柄/键盘同时可用
（底盘键缺省不驱动，互不干扰）。

流程（顺序重要）：

1. 从臂按手柄 **START** 收到休息位（或确认它在休息位附近）；
2. **主臂手动摆成同样的休息姿态**（折叠收拢）；
3. GUI：**连接主臂 → 对齐零位 → 开始跟随**；
4. 停止跟随即回手柄/摇杆控制，衔接无跳变。

对齐零位代替主臂标定——两臂同型号，差值映射即可；主臂拉超程时从臂被标定限位夹住。
Rust 端串口 worker 与 ZMQ worker 同一铁律：独立线程，不进 Tauri runtime。

## 机械臂标定

见项目根 `README.md` 的「机械臂标定 Arm calibration」一节（先按 START 收臂松弛，再停服务跑 `lerobot-calibrate`）。

## 架构要点（为什么这么写）

- **ZMQ 在 Rust，不在前端**：WebView 开不了 ZMQ socket（ZMQ 不是 WebSocket）。
  Rust 后端 (`src-tauri/src/main.rs`) 持有一个 PUSH socket，前端通过 Tauri `invoke` 调。
- **线协议**（对齐 lerobot 0.5.2 `lekiwi_host`/`lekiwi.py`）：
  host 在 `tcp://*:5555` bind 一个 PULL socket，每条命令是一段 JSON：
  `{"x.vel": m/s, "y.vel": m/s, "theta.vel": deg/s}`。host 过滤 `.vel` key 丢进
  `_body_to_wheel_raw`。三个 key 必须都在。
- **持续发 + 松手发零**：host 有 idle watchdog，命令断流就停车。前端 20Hz 流式发，
  松开所有键立刻补一条零速；失焦/切 Tab/隐藏都发零（dead-man）。
- **速度三档**严格对齐 lerobot 默认：`0.1/0.25/0.4 m/s`、`30/60/90 °/s`。
- **纯 Rust zeromq crate**：不装 libzmq 系统库，`cargo build` 即可。

## 结构

```
gui/
├── run.sh                    # 编译并启动
├── src-tauri/
│   ├── Cargo.toml            # + zeromq (纯 Rust) + tokio(time)
│   ├── src/main.rs           # zmq_connect / zmq_send_base / zmq_disconnect / zmq_status
│   └── tauri.conf.json
└── ui/
    ├── index.html            # 两个 tab + ZeroMQ 遥控界面
    ├── style.css             # 醒目 tab + 键盘/速度/可视化样式
    └── js/
        ├── state.js          # $ / S / invoke
        ├── main.js           # tab 切换
        └── zmq.js            # 连接 + 键盘遥控 + 方向可视化
```
