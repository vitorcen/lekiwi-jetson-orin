---
name: lerobot-installed-orin
description: 板子已装 lerobot 0.5.2@26ff40d（conda env lerobot/py3.12）— GitHub 要走 ghfast.top 镜像；PyPI 的 torch 是 cu130 用不了 GPU
metadata:
  type: project
---

Orin 板子（192.168.3.188）**已装好 lerobot**（2026-07-16）：

- conda env **`lerobot`**（python 3.12），miniconda 在 `~/miniconda3`（`auto_activate_base false`）。
  用前 `source ~/miniconda3/etc/profile.d/conda.sh && conda activate lerobot`。
- 代码在 `~/lerobot`，**浅克隆**、detached HEAD 锚定 `26ff40d`（= 版本 **0.5.2**）。
- 装了 `.[lekiwi]` extra + `pygame` + `jupyterlab`。CLI 齐全：`lerobot-find-port` /
  `lerobot-calibrate` / `lerobot-eval` / `lerobot-find-cameras` …
- 实测 lerobot 自己的 `FeetechMotorsBus` 能读到全部 9 个电机（`/dev/ttyACM0`，见
  [[servo-bus-ch341-bringup]]）。

**坑 1 — GitHub 拉不动**：板子 `curl https://github.com` 返回 200，但 `git clone/fetch`
必挂（`GnuTLS recv error` → `early EOF` → 最后 443 直接 timeout）。网页通≠git 通。
解法：**加 `ghfast.top` 前缀**，实测秒过：
```bash
git remote set-url origin https://ghfast.top/https://github.com/huggingface/lerobot.git
git fetch --depth 1 origin <完整 40 位 sha>   # 按 SHA 浅拉，只要那一个 commit
```
镜像可达性实测：`ghfast.top` ✅、`ghproxy.net` ✅、`gitclone.com` ✅；
`hub.gitmirror.com` ❌、`kkgithub.com` ❌。**本机（vitor-desktop）到 github/codeload 都通**，
镜像全挂时可本机拉完 rsync 过去。

**坑 2 — PyPI 的 torch 用不了 GPU**：`pip install -e ".[lekiwi]"` 装进来的是
**torch 2.11.0+cu130**，而 JetPack 6.2.2 的驱动是 **CUDA 12.6** →
`torch.cuda.is_available() == False`（报 "NVIDIA driver is too old, found 12060"）。
遥操作/标定只用 CPU，**不影响**；等到跑 ACT 等策略推理时，必须换 **NVIDIA 官方
Jetson torch wheel**（JetPack 6.x / CUDA 12），别用 PyPI 的。届时还要开 Super 模式
（见 [[jetson-platform-baseline]]）。

**How to apply:** 板子上装任何 pip 包前先 `conda activate lerobot`（pip 已配清华源）。
apt 层前提见 [[jetson-apt-network-cn]]。机器人拓扑与方案见 [[lekiwi-robot-target]]。

**踩坑备忘**：`pgrep -f <脚本名>` 判断后台脚本是否在跑会**假阳性** —— ssh 执行的命令行里
含该字符串，pgrep 匹配到自己。用 `ps -ef | grep xxx | grep -v grep` 或直接查日志 marker。
（同类坑见 [[jetson-apt-network-cn]] 里的 `pkill listener` 误杀 sshd。）
