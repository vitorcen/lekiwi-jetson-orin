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
| 🤖 **ROS 2** | 占位 | 后续：把 LeKiwi 包成 ROS 2 节点、建图导航等 |

## ZeroMQ Tab 怎么用

1. **先在 Orin 上起 host**（车要通电、9 电机在线）。有两个选择：

   **A. 底盘专用 host（推荐，免标定）** — 只驱动 3 个轮子，立刻能开车：
   ```bash
   scp gui/board/base_host.py gui/board/*.sh jatson@192.168.3.188:~/   # 首次部署
   ssh jatson@192.168.3.188 'bash ~/start_base_host.sh /dev/ttyACM0'   # 起（停：stop_base_host.sh）
   ```

   **B. 原版 lerobot host（要先标定机械臂）** — 底盘+臂都能用，但 `connect()` 会强制
   交互式标定（手摆姿势按回车），必须在能交互的终端里先跑一次
   `lerobot-calibrate --robot.type=lekiwi --robot.port=/dev/ttyACM0 --robot.id=orin_kiwi`，
   之后才能 `python -m lerobot.robots.lekiwi.lekiwi_host ...`。**SSH 非交互下直接跑 host 会 EOFError。**

2. GUI 里填 Orin IP（默认 `192.168.3.188`）和命令端口（默认 `5555`），点**连接**。
3. **点键盘区**获取焦点（虚线框变实线高亮），然后：

   | 键 | 动作 | | 键 | 动作 |
   |---|---|---|---|---|
   | W / S | 前进 / 后退 | | Q / E | 左转 / 右转 |
   | A / D | 左移 / 右移（平移） | | R / F | 升 / 降速档 |
   | 空格 | 急停 | | | |

4. 松手即停；切走 Tab、窗口失焦、页面隐藏都会自动停车（dead-man）。

**先把底盘架空再试。**

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
