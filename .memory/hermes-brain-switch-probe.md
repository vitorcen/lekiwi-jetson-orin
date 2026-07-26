---
name: hermes-brain-switch-probe
description: How the /brain preset switch probes a model, and the gateway gotcha that masks provider errors
metadata:
  type: project
---

P2 大脑 preset 切换（voice-daemon `POST /brain`，事务见 [[agent-voice-pages-plan]] §5.5）落地后的两条硬事实：

**1. Hermes 网关把 provider 的 HTTP 4xx 伪装成成功。** 错模型名 / 错 key 时，DeepSeek 端返回 HTTP 400，但网关**不发 `error` SSE 事件**——而是把错误串塞进 `assistant.completed` 的 `content`（如 `"HTTP 400: The supported API model names are ..."`），`partial:false completed:true`，且**零 `assistant.delta`、`run.completed.usage.output_tokens=0`**。因此探针「过」的判据**不能**是「收到 assistant.completed」，必须是**收到过 `assistant.delta`，或 completed 且 `output_tokens>0`**；否则把 completed 里的 content 原样当失败原因报出（正好带 HTTP 4xx 文本）。这是 P2 验收门核心，`voice/daemon.py::_brain_probe` 就这么写的。

**Why:** 端口 200 ≠ 模型可用；网关的错误呈现方式是反直觉的（成功事件里装错误）。P4 Omni 探针会踩同一个坑。

**2. 网关重启就绪要 >20s。** `systemctl --user restart hermes-gateway-robot` 返回后，网关还要重新 spawn profile 里的 MCP 子进程（vlm + drive 两个 venv python），`/health` 就绪常需 20–30s，连发切换更慢。`HERMES_READY_TIMEOUT` 定 30s；超时会诚实还原备份、报 `gateway not ready`。

**3. `hermes model` 向导写的 yaml 是另一种风格，`model.base_url` 是全局路由陷阱。**
向导切模型（如用户手切 mimo-v2.5）写出 `model.provider: xiaomi`（内置 provider 名，无
`custom:` 前缀）+ `model.base_url: https://api.xiaomimimo.com/v1` 内联端点，**不建
providers 块**。`base_url` 对**所有**模型全局生效——补丁器若不管它，切回 deepseek 时
残留的 base_url 会把请求悄悄路由到小米端点。修法（已落 `voice_brain.py`）：base_url
纳入可变集、补丁时一律删除，端点只归 `providers.<name>.api` 管。2026-07-21 已实测
mimo↔deepseek 真实双向往返全过（探针拿真 token），mimo preset 真值：api
`https://api.xiaomimimo.com/v1`、model `mimo-v2.5`、key_env `XIAOMI_API_KEY`。

**4. 探针永远是网关重启后的第一个请求，冷启比热态慢 9 倍。**
切换流程是「打补丁 → 重启网关 → 探针」，所以探针**必然**吃冷启动成本，而
`/health` 返绿 ≠ agent 路径已热。2026-07-26 实测（同一请求，跨 `systemctl restart`）：
**冷 15.3s / 热 1.7s**。其中约 5s 是 hermes 去 `openrouter.ai` 取模型元数据 ——
这块板子连不通，SSL 握手直接 EOF，得等重试耗尽才轮到问模型；它在**进程内**缓存，
所以之后每次都快。原来 `HERMES_PROBE_TIMEOUT=10` 于是**永不可能过**：10s 在初始化中途
就到期、daemon 删了 session，而模型 2s 后照常答出来（网关日志 `latency=2.6s`）。
现象是每次切云端大脑都以 `probe failed: timeout >10s` 回滚，而 provider 完全健康。
已改 **60s**。30s 够云端，但**不够局域网本地模型**：那边探针的第一次调用还要
换权重（`mlx_lm.server` 按请求的 `model` 换入并卸掉上一个），9B→35B 要读 35GB。
实测同一次切换：探针窗口 **25.7s**（模型那一下 17.4s），距 30s 只剩 4s，
而且那还是权重刚下完、page cache 是热的。冷盘会更慢 —— 炸了的后果是
**把一个完全健康的配置回滚掉**，这比慢报错糟得多。60s ≈ 实测最坏值的 2.3 倍。

**How to apply:** 探针失败先看**网关日志**再看 provider —— 日志里有 `latency=` 和真实
答复长度，能一眼分清「模型不行」和「我们等太短」。这类"重启后第一次特别慢"的超时，
别去加预热请求（预热本身就要付那一次冷启，总时间不变还多一套机制），
把超时定对就行。相关：[[board-memory-ceiling]]（同样是「上限设在真实成本之下」）。

**How to apply（探针判据）：** 改探针 / 加新大脑（mimo、omni）时沿用 delta/output_tokens 判据，别信 completed。用户手动跑过 `hermes model` 向导后，yaml 会回到 base_url 风格并产生 drift——GUI 切一次任意 preset 即归一。切换单测在 `tests/test_voice_brain.py`（yaml 补丁 + preset 校验），跑：`uv run --with pytest --with numpy --with ruamel.yaml pytest tests/ -q`（比 [[unit-tests-board]] 多一个 `--with ruamel.yaml`，voice_brain 用 ruamel round-trip 保 config.yaml 其余字节/注释原样）。
