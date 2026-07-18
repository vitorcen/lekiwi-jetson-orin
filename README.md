# LeKiwi on Jetson Orin Nano

LeKiwi 移动操作机器人(3 轮全向底盘 + SO-101 从臂,Feetech STS3215 总线舵机 ×9)
跑在 Jetson Orin Nano 8GB(JetPack 6.2.2)上,软件栈 lerobot 0.5.2。

| 目录 | 内容 |
|---|---|
| `gui/` | Tauri 桌面控制台(键盘遥控)+ 板载服务(`board/`:base_host、手柄 daemon、systemd 单元) |
| `docs/` | 玩法总览、遥操作实施方案(HTML,可离线浏览) |
| `notebooks/` | 手动试车 notebook |
| `.memory/` | 项目长期记忆(协议见 `.memory/SKILL.md`) |

## 快速开始 Quick start

板子(`jatson@192.168.3.188`)上 `base_host` 与 `pad_teleop` 已做成 systemd 开机自启:
手柄接收器插板即可遥控;桌面端 `cd gui && ./run.sh` 起 GUI 键盘遥控。
键位、部署、架构详见 `gui/README.md`。

## 机械臂标定 Arm calibration(lerobot-calibrate)

臂关节标定用 lerobot 自带 CLI,流程 = **摆中间位 → 回车 → 每关节手动拉到最大最小 → 回车确认**。
需要交互终端,在板子上跑:

```bash
# 1) 先按手柄 START 收臂松弛(臂折回休息位并断扭矩),再释放串口
ssh -t jatson@192.168.3.188
sudo systemctl stop base_host pad_teleop

# 2) 标定(lerobot conda env)
conda activate lerobot
lerobot-calibrate \
  --robot.type=lekiwi \
  --robot.port=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61036495-if00 \
  --robot.id=orin_kiwi \
  --robot.cameras='{}'
# --robot.cameras='{}' 必须带：lekiwi 默认配置含 front/wrist 两个相机，
# 没插相机时 connect() 会先在开相机处崩掉；标定不需要相机。

# 3) 恢复手柄遥控
sudo systemctl start base_host pad_teleop
```

交互两步:

1. 提示 *Move robot to the middle of its range of motion* —— 此时臂扭矩已松(**扶住,会掉**),
   手动把 6 个关节摆到行程中间的标准姿态,回车(定零位);
2. 屏幕实时刷各关节 min/max —— 把每个关节依次拉到两端极限(到位即可,别硬顶),
   全部过一遍后回车确认。轮子 7/8/9 连续旋转,不参与。

结果写入 `~/.cache/huggingface/lerobot/calibration/robots/lekiwi/orin_kiwi.json`。
标定后原版 `python -m lerobot.robots.lekiwi.lekiwi_host` 才能启动(它强制要标定),
完整 lerobot 流程(leader 遥操作 / 录数据 / 跑策略)随之解锁;base_host/手柄遥控本身不依赖标定。
