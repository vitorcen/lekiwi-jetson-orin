---
name: board-account-jetson
description: 板子账号 2026-07-20 由 jatson 改名为 jetson(usermod -l,非拷贝);改名踩到的三类"文本 grep 扫不到"的引用
metadata:
  type: project
---

2026-07-20 板子账号从错字 `jatson` 改名为 `jetson`,仓库目录同步 `lekiwi-jatson-orin` →
`lekiwi-jetson-orin`,GitHub 仓库也已改名。现状:`jetson` uid/gid **1001**(不是 1000),
家目录 `/home/jetson`,组、linger、ssh key 全部沿用。

**Why 用 `usermod -l jetson -d /home/jetson -m jatson` 而不是"建新号+拷数据"**:
家目录 17GB(miniconda3 7G / work 6.2G / .hermes 2.1G / models 1.5G),拷贝法要重打 7 个组、
重发 ssh key、enable-linger、修属主,而**两条路都得改同一批硬编码 `/home/jatson` 字符串**——
省下的全是纯风险。幸运前提:`/home/jatson` 与 `/home/jetson` **等长(12 字符)**,
`lekiwi-jatson-orin` 与 `lekiwi-jetson-orin` 也等长,所以原地字节替换不改变任何文件长度
(实测替换前后全部文件 size 的 md5 一致)。

**How to apply — 改名/搬家时,`grep -rlI` 会漏掉三类引用,每一类都单独炸过一次**:
1. **符号链接**:链接目标不是文件内容,`grep` 永远扫不到。板上有 28 个,包括
   `vlm/.venv/bin/python`、`.hermes/hermes-agent/venv/bin/python`、`.local/bin/uv`、
   `~/.config/systemd/user/default.target.wants/*.service`。修法:
   `find <root> -xdev -type l -lname '*old*'` 逐个 `ln -sfn` 重指。
2. **ELF 里的 RUNPATH**:`llama-server` 的 RUNPATH 硬编码在二进制里,报
   `libllama-server-impl.so: cannot open shared object file`。板上没有 patchelf,
   等长时用 `perl -0777 -pi -e` 按字节整块替换(`-0777` 不做行切分,二进制安全)。
   conda `envs/lerobot/lib/*.so` 同样带旧 RUNPATH。
3. **`/etc/subuid`、`/etc/subgid`、GECOS 注释字段**:`usermod -l` 不动它们,要手工 sed。

**顺带挖出一个潜伏 bug(已修)**:`base_host.service` 同时写了 `WantedBy=multi-user.target`
和 `After=multi-user.target`,构成环
`multi-user.target → pad_teleop →(After)→ base_host →(After)→ multi-user.target`。
systemd 静默砍掉 `pad_teleop` 的启动任务来破环,现象是**手动 start 能起、开机永远不起**,
且 `journalctl -u pad_teleop` 什么都没有(日志挂在 `multi-user.target` 名下,要 `journalctl -b |
grep "ordering cycle"` 才看得见)。被 target 拉起的服务不能再 `After=` 这个 target。

板子 sudo 密码位置见 [[board-sudo-keychain]]。
相关:[[lekiwi-pad-teleop]] [[vlm-stack-orin]] [[voice-frontend-s2]] [[board-memory-ceiling]]
