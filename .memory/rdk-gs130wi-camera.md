---
name: rdk-gs130wi-camera
description: 手上的 "RDK CS130WI" 相机 = 官方 GS130WI 双目一体模组（双 SC132GS + ICM-42688-P），Jetson 无原生驱动，移植方案与实施仓库已就位
metadata:
  type: project
---

2026-07-18：板子双 CSI 口接的「RDK CS130WI」实为官方 **GS130WI 双目一体模组**——
右排线 = 右目 + IMU（ICM-42688-P @0x68，i2c-2 上已实测应答），左排线 = 左目；
双 SmartSens SC132GS（1.3MP 全局快门，芯片 ID `0x3107/08 == 0x0132`；**实测彩色 BGGR
Bayer、无 IR-cut**——RDK X5 同款模组 pre-ISP RAW 2×2 相位统计定谳，非单色），排线布 2 lane
但 **X5 实机 1088×1280 表走 1-lane@1.2Gbps**（896×896 才 2-lane，DT bus-width 跟表），
模组自供电自时钟（**排线 Pin1=GND/Pin22=3V3，与 Orin Nano 载板相反；18 脚是 FSYNC
不是 MCLK**——电气核对没过之前不许直插上电）。

**引脚映射（纸面逐 pin 已核，22 里 21 干净镜像）**：Jetson J20/J21 = Molex 54548-2272，
Pin1=3.3V→Pin22=GND，与模组（Pin1=GND→Pin22=3V3）互为镜像；按反序（模组 m ↔ Jetson 23−m）
电源/GND/I2C/2-lane 数据+时钟差分（N↔N、P↔P）全严丝合缝，模组 RESET(17)↔Jetson CAM_PWDN(6)
正好当复位。**唯一非镜像脚 = pin 18**：模组 FSYNC(输入) ↔ Jetson CAM0_MCLK(输出)——**不是短路不烧**
（均 1.8V 信号），DT 里不配 mclk、该脚留 GPIO 即可（自由跑表本就忽略 FSYNC；将来硬同步可反过来用这脚驱 FSYNC）。
模组 pin 11/12/14/15 是 **NC 空脚**，反序后盖在 Jetson CSI0 上 → **CSI0 整组空用**，模组 lane 落 **CSI1 brick**
（设备树只配 CSI1）。**仍待实测**（非已验证）：镜像对得上是纸面，实物该用同面/异面 FPC 取决于连接器接触面，
插反=3V3 撞 GND 会烧——上电前必须万用表验反序连通（Jetson pin1↔模组 pin22 通=对；↔模组 pin1 通=直通线，停）。

**Why:** Jetson L4T 没有 SC132GS 驱动、这颗 sensor 没有 Argus ISP tuning（且无 IR-cut
色彩没救）——官方只支持 RDK X5。移植 = 用地瓜开源寄存器表 + Rockchip sc132gs.c 参考 +
tegracam 框架重写驱动，一期走 Bayer BG10（BGGR 如实申报）V4L2 直读，应用侧去马赛克或
2×2 合并出亮度图。**两个 X5 实测降险点**：① 库存 30fps 表是外触发模式（vts=0x3fff），
Jetson 上 FSYNC 关死会零帧——必须自由跑表（`0x3222=0x00`，X5 热打实证）；② 排线上
0x50 EEPROM 有出厂双目标定（"UNION"，基线 69.7mm，解析见 RDK-experience 仓
stereo_cam.py parse_calib），免自标定。X5 实测 sensor 地址：右 0x30、左 0x32。

**How to apply:** 完整方案（分阶段计划 + 风险清单，经 codex 评审修订）在
`docs/sc132gs-jetson-csi-port.html`；实施代码放 submodule
`dependencies/RDK-GS130WI-Jatson-Driver`（vitorcen/RDK-GS130WI-Jatson-Driver）。
P0 电气验证（net-to-net 映射、限流上电、芯片 ID 实测）通过前不写驱动代码。
相关：[[jetson-platform-baseline]]、[[lekiwi-robot-target]]。
