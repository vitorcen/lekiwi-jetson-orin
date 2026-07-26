---
name: local-llm-brain-mac
description: Mac 上跑普通 LLM 当机器人大脑（mlx_lm.server + Qwen3.6-35B-A3B / Qwen3.5-9B），以及本地模型独有的三个延迟/配置陷阱
metadata:
  type: project
---

2026-07-26 起：Agent 页多两条大脑 `local-35b` / `local-9b`，Mac 上一个
`mlx_lm.server` 同时供这两个模型。启动脚本 `scripts/mac_llm_server.sh`（:8094）。
权重在 **HF 默认路径** `~/.cache/huggingface/hub`，和 omni 那个 22GB 的
`mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit` 同一个目录。
`Qwen3.6-35B-A3B-8bit` 35GB / `Qwen3.5-9B-8bit` 9.8GB。

## 这不是新的大脑种类

本地 LLM = **一条普通 hermes preset**，只是 `api` 指局域网。于是白拿全套：
MCP 工具、SOUL 提示词、切换事务、探针、ASR→LLM→TTS。和 omni 那条
「音频进 / 原生语音出 / 没有 TTS」的旁路完全不同。
一个 server 供两个模型：`mlx_lm.server` 按请求里的 `model` 字段
`ModelProvider.load()` 换入并卸掉上一个 —— 正好对上「preset = api + model」。

## 三个只在本地模型上存在的坑

**1. 思考模式默认开，开着等于哑巴。** Qwen3.5/3.6 的 chat template 在
`enable_thinking` 缺省时走思考。问「又下暴雨了。」，9B 把 300 token 全烧在英文
内心独白（`"Here's a thinking process that leads to the suggested response:"`）
上，`content` **返回空**。必须 `--chat-template-args '{"enable_thinking":false}'`。
关掉后 0.3–0.5s 出话。工具调用不受影响（吐 tool_call 时本来就不纠结）。

**2. prompt 长度就是延迟 —— 云端模型不是这样。** 网关日志实测（9B）：

| in= | cache 命中 | latency |
|---|---|---|
| 54990 | 22% | **24.8s** |
| 65890 | 83% | **11.8s** |
| 65934 | 100% | **1.2s** |

会话越用越长（`tool_turns=38`，每次 `vlm_look` 的画面描述都进历史，涨到 75k），
没命中的那截要真算一遍，约 1k token/s。deepseek 同样 12k prompt 3.9s 且不在乎长度。
**长会话会越来越慢，开新会话立刻回 1s**；脏会话还会让模型答非所问
（35B 在 75k 脏历史上回「好嘞，主人！」，全新会话上正常调 vlm_look 看窗外）。
服务端配了 `--prompt-cache-size 4 --prompt-cache-bytes 8G`，100% 命中那条是它挣的。

**3. `mlx_lm.server` 没有任何鉴权。** 没有 `--api-key`，源码里没有 Authorization
校验。所以 preset 里的 `LOCAL_LLM_KEY` 是**占位符不是密钥**（hermes 强制
`key_env` 非空才放行切换，写在板子 `~/.hermes/profiles/robot/.env`）。
8094 别映射公网。这也是 `voice_brain._check_api` 只对**私网 IP** 放行 http 的原因。

## 质量与延迟（2026-07-26 实测，全新会话）

- **9B**：首 token 0.6–0.8s，整句 ~1s。工具调用 100% 对（含 enum 参数）。
- **35B-A3B**：0.3–0.9s（MoE 激活 3B，比 9B 还快），回答明显更好 —— 有人设有
  上下文（「不过咱们还是专注眼前的路吧」这种 9B 给不出）。
- 两个都远好于 omni（omni 那次是历史锁死，见 [[omni-brain-two-stage]]）。

**How to apply:** 换模型只改 preset 的 `model` 字符串，服务端自己换入。
新模型先单独 `curl /v1/chat/completions` 验 tool_calls 再进 preset。
测延迟一定用**全新 conversation/session**，脏会话量出来的是 prefill 不是模型。
相关：[[hermes-brain-switch-probe]]（切换探针预算）[[vlm-stack-orin]]
