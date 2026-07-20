---
name: board-sudo-keychain
description: 板子 jetson 的 sudo 密码存在 Mac 钥匙串,取用方式;绝不写进仓库
metadata:
  type: reference
---

板子 `jetson` 账号的 sudo 密码存放在**开发机 macOS 钥匙串**里,不在仓库、不在 memory、
不在任何文件:

```
service: lekiwi-board-192.168.3.189
account: jetson
读取:    security find-generic-password -a jetson -s lekiwi-board-192.168.3.189 -w
```

**Why**:`.memory/` 随仓库提交,git 历史不可逆,密码一旦写进去就等于公开。钥匙串是本机的、
不跟随 git,同时又能让工具按需自取,不用每次问人。

**How to apply**:需要 root 时把密码**只经 stdin** 喂给 `sudo -S`,不要出现在命令行参数里
(命令行会进 shell history 和 `ps`):

```bash
security find-generic-password -a jetson -s lekiwi-board-192.168.3.189 -w \
  | ssh jetson@192.168.3.189 'sudo -S -p "" bash /tmp/somescript.sh'
```

注意 `sudo -S` 只吃掉 stdin 第一行,所以脚本要用**文件**传(`bash /tmp/x.sh`),
不能用 `bash -s` 走 stdin——那样脚本会跟密码抢同一个流。

无密码可用的窄权限另有一条:`/etc/sudoers.d/lekiwi-deploy` 给了 `jetson`
NOPASSWD 的 `systemctl restart/stop/start base_host|pad_teleop` 和 `daemon-reload`,
`scripts/deploy_board.sh` 靠它免密重启服务。相关:[[board-account-jetson]]
