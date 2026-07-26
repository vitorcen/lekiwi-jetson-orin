---
name: board-memory-ceiling
description: 板子 8GB 已见底(实测空闲 195MB / swap 2.2GB)——llama-server 独占 3.4GB;fork 变慢会伪装成各种"设备故障",排查前先看 free
metadata:
  type: project
---

2026-07-20 实测板子(Orin Nano 8GB)内存已经吃满:总 7607MB / 已用 6966MB /
**空闲 195MB**,swap 3803MB 里用掉 **2228MB**。占用排行:`llama-server` RSS 3.4GB
(换出 1GB)、`voice/daemon.py` 647MB(**自己被换出 327MB**)、桌面会话
(gnome-shell 261MB + Xorg 112MB + WebKit 161MB + gnome-software 换出 100MB)、
`hermes` 111MB。没有 OOM kill,所以 `journalctl` 里什么都看不到。

**Why**:这个状态下,一个自身部分被换出的进程 `fork()+exec()` 一个小命令可以拖过
数秒。后果是**内存问题伪装成设备问题**——voice daemon 的 `_discover_card` 跑
`subprocess.run(["arecord","-l"], timeout=5)` 超时,被 `except: return None` 当成
"声卡不存在",于是 `audio_ok=False`、采集停、播放拒绝,feed 里刷 "audio device
missing" 刷了 81 秒;同期日志伴随 asyncio 的 `Unknown child process pid X, will
report returncode 255`(子进程回收错乱)。而同一时刻在 ssh shell 里连测 60 次
`arecord -l` 全中——**小进程 fork 得起,大进程 fork 不起**,所以手工验证会得出
"命令没问题"的错误结论。

**How to apply**:
1. 板端任何"设备时有时无""命令超时""子进程回收警告",**先 `free -m` 和
   `ps -eo rss,comm --sort=-rss | head`**,别急着查设备和线缆。
2. 写板端 daemon 时,**探测超时 ≠ 目标不存在**。把两者合并成同一个返回值会让程序
   把好好的通路拆掉。voice daemon 已按此拆成 `ProbeFailed` 异常(探测跑不动就保留
   现有卡不动),超时也从 5s 放宽到 15s。
3. 要腾内存,优先级:不用视觉时停 `llama-server`(GUI 视觉 Tab 有停止按钮,省
   3.4GB)> 关板子桌面会话(约 630MB,但接显示器操作会被关在门外,须先确认)。

## MemoryHigh 设在稳态之下 = 一条永久刹车,而且不留任何日志

2026-07-26 又栽了一次,这次不是系统内存不够,是**自己给自己设的 cgroup 软上限**。
`voice-daemon.service` 写着 `MemoryHigh=900M`,注释还是 sensevoice 年代的
"稳态 RSS ~955MB";换到 qwen3-asr + melo + matcha 之后真实稳态是 **anon 2350MB**。

`MemoryHigh` 超了**不杀进程**,而是每次分配都走直接回收、把匿名页压进 swap。设在稳态
之下就是让 1.4GB 热页永远换出换入:16 小时里 `memory.events` 的 `high` 计到
**1050 万**、`pgmajfault` **3874 万**、`workingset_refault_anon` **2766 万**。
症状是**没有 OOM、没有日志、进程活着**,只是一个纯内存读的 HTTP handler 要 **40 秒**
才回(修好后 **2.6ms**),对外表现成"麦克风识别率突然特别低"。

**How to apply**:
- 板端 daemon 慢/超时/丢事件,`free -m` 之后**紧接着看 cgroup**:
  `systemctl --user show <unit> -p MemoryCurrent -p MemoryHigh`,
  再看 `/sys/fs/cgroup/.../<unit>/memory.events` 的 `high` 计数。**非零就是在挨节流**,
  这个计数比任何日志都直接。
- 改上限前先**量稳态**(`memory.stat` 的 `anon`),别信注释里的旧数字。量的时候要把
  上限临时设成 infinity,否则量到的永远是上限本身。
- 分清一次性抬升和泄漏:固定负载跑 90 秒看 `anon` 动不动。本次 1486→2350MB 看着像
  泄漏,实际是解封后热页回填,90 秒 281 chunk/14 段 anon 纹丝不动。
- 上限是**跑飞护栏**不是运行点:留 25%+ 余量(现设 3G),给引擎热切换期两套模型同时在。

相关:[[voice-frontend-s2]] [[vlm-stack-orin]] [[lekiwi-gui-tauri]] [[mac-side-speech]]
