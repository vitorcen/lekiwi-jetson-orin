---
name: mlx-thread-local-stream
description: MLX/mlx-vlm 的 generation stream 是线程局部的——主线程加载、worker 线程推理会炸，且只炸 talker 让人误判为声码器 bug
metadata:
  type: feedback
---

把 MLX 模型跑进线程池（比如包成 HTTP 服务、单 worker 串行推理）时，
**必须让 import、权重加载、推理发生在同一个线程**。

`mlx_vlm/generate/common.py` 用 **`mx.new_thread_local_stream(mx.default_device())`**
在**模块导入时**创建 generation stream。若在主线程 `load()` 而在 worker 线程推理，会报：

```
RuntimeError: There is no Stream(gpu, 1) in current thread.
```

**Why:** 2026-07-25 做 omni 大脑的 Mac 服务端时踩到。最坑的是**故障是局部的**：
thinker 照常出文本、工具调用一切正常，**只有 talker/code2wav 崩**——
看起来像声码器或 mlx-vlm 移植的 bug，实际是线程归属问题。排查方向会被带偏很远。

**How to apply:** 不要用锁或 `mx.stream(...)` 去绕。正确做法是
`ThreadPoolExecutor(max_workers=1)` 建好后，**把模型加载也 submit 进去**
（`self.model, self.processor = pool.submit(_load).result()`），
让那个线程独占 import + 权重 + 推理。反正单模型服务本来就必须串行，
这样线程局部不变量按构造成立，而不是靠约定维持。

## 连带教训：并发/取消类测试必须断言「被测对象此刻确实在跑」

同一次排查里，talker 一崩，聊天轮从 ~2.5s 缩到 ~700ms。于是
「sleep 1 秒后发第二路请求应得 503」和「取消正在跑的请求」**两个用例同时变成假阳性**——
worker 早空了，503 没触发、cancel 报 `was_active: false`，
但表面上看是「并发和取消功能坏了」，掩盖了真正的根因。

所以这类用例必须显式断言前置条件：`check("被拒时首路仍在跑", not task.done())`。
否则它们测的不是并发，而是自己的 sleep 时长——**用例的有效性依赖被测系统正常，
这是不可接受的循环依赖**。

相关：[[omni-brain-two-stage]] [[gui-disabled-swallows-clicks]]
