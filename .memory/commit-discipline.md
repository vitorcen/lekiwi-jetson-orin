---
name: commit-discipline
description: 实施期间不 commit 不 amend——改动留工作区,用户发话收尾才落一笔;「避免细碎 commit」≠「随手 amend」
metadata:
  type: feedback
---

2026-07-20 用户纠正:实施过程中我每完成一步就 `git commit --amend` 进特性 commit,
用户指出「实施无需着急 commit,等用户指示」。

**Why**:amend 会把不相关的后续改动(评审修复、新需求)混进已成形的 commit,用户
失去对 commit 边界的控制;工作区保持 dirty 反而让用户随时能 diff/取舍。「避免细碎
commit」的本意是最终产出一笔原子 commit,不是过程中持续吸附。

**How to apply**:代码改完、部署验证完就停在工作区;只有用户明说「commit」「收尾」
「合一笔」时才动 git。用户指定和哪个 commit 合并时才 amend/reset --soft。
相关:[[unit-tests-board]]。
