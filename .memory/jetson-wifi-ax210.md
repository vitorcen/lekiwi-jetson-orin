---
name: jetson-wifi-ax210
description: Orin 的 Intel AX210 Wi-Fi 6E —— L4T 没编 iwlwifi，装 backport-iwlwifi DKMS，坑在 cfg80211 符号冲突
metadata:
  type: reference
---

板子 WiFi 是 **Intel AX210 Wi-Fi 6E**（PCIe 2725:0024，网卡名 `wlP1p1s0` 不是 wlan0）。
2026-07-18 启用：**硬件+固件都在，唯独 L4T `5.15.185-tegra` 内核没编 `iwlwifi` 模块**
（`modinfo iwlwifi` 找不到 = 关键判据），所以设置里没 Wireless。

解法：`sudo apt install -y backport-iwlwifi-dkms`，DKMS 对着已装的 `linux-headers`
现编 `iwlwifi/iwlmvm/mac80211/cfg80211` 四件套到 `updates/dkms/`（不用重编内核）。

**唯一的坑**：装完 `modprobe iwlmvm` 报 `Invalid argument`——开机常驻的**内核自带
`cfg80211`**（refcount 0）与 backport 的符号版本 CRC 对不上。修：
`sudo modprobe -r cfg80211 && sudo modprobe iwlmvm`（免重启）；卸不掉就 `sudo reboot`
（depmod 后 backport 优先加载）。内核升级后无线消失先看 `dkms status`，需要时 `dkms autoinstall`。

完整过程（诊断→装→坑→验证→持久化）写在 **`docs/orin-wifi6e-ax210-install.html`**，
主刷机文档 `docs/orin-nano-super-install.html` 已交叉链接。蓝牙（8087:0032，走 btintel）
本就正常，与本条无关。相关：[[jetson-platform-baseline]]、[[servo-bus-ch341-bringup]]。
