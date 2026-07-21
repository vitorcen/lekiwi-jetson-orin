---
name: voice-asr-engines
description: 语音 ASR 引擎阵容 + 流式模式:qwen3-asr 抗噪主力、四离线+四流式可热切换、两级选择;加/换 ASR 前必读
metadata:
  type: project
---

voice-daemon 的 ASR 做成**数据驱动多宿主 + 流式模式**(2026-07-21)。改/加 ASR 前读这条,别重复踩坑。

**离线引擎**(`voice_engines.REGISTRY["asr"]`,VAD 切段→`transcribe(whole_seg)`,进程内热切换,载新→卸旧):
- `qwen3` = **Qwen3-ASR-0.6B int8,sherpa 原生 `from_qwen3_asr`**(板上 sherpa 1.13.4 就有,不用 torch/GPU)。LLM-ASR,**带噪鲁棒性碾压小模型**(0dB WER ~7% vs SenseVoice ~13%),延迟 ~1.5s/短段。**RSS 随音频长度涨**:1-3s 段 ~0.9-1.2GB,20-50s 长音频才飙 3.5GB(KV cache);decoder `max_total_len=512`,>10s 会截断。**这是抗噪主力/默认**。
- `sensevoice`/`paraformer` = NAR 小模型(~230MB, <1s),多语种(sensevoice zh-en-ja-ko-yue),但**近场噪声下都马马虎虎**,实测半斤八两。
- `whisper` = large-v3-turbo int8(~1GB, RSS 1.4G),**RTF~2 太慢**(2s 音频要 4.3s),淘汰。

**流式模式**(`voice_config` stream 轴 `{enabled,model,endpoint_silence_s}`,`StreamingAsr`+`STREAM_SPECS`,`OnlineRecognizer.from_transducer`,**免VAD 自带端点**,`feed()→(partial,final)`,DEBUG-only 不进对话链路)。二级可选模型:
- **`x-asr-zh-en`(第一位/默认,现场实测略优于 xlarge;2026,160M 参数/100万小时数据,中英混
专长,fp32 593MB 无官方 int8,板上 RTF0.25 载9s)**、`zh-xlarge`(700M 参数、载入 ~24s、纯中文、RTF0.56)、**`para-zh-en`
(DAMO Paraformer-large 流式,中英,int8 226MB,实测 RTF0.115 载2s;api=paraformer 走
from_paraformer 无 joiner)**、`zh-2025`、`multi-zh`、`zh-en`(弱双语基线)。
- **两遍法(codex 终审认可,待实施)**:流式只出 provisional partial(只显示,不进大脑/不触发动作),
端点后把段喂离线 qwen3 出权威 final——同时解决同音字(在/再)和中英混。**流式 LLM 上板无解**:
Fun-ASR-Nano 0.8B llama.cpp 路径=VAD 整段(真流式仅 vLLM,Orin 跑不了),FireRed-LLM 8.3B 等全塞不下。
- **Fun-ASR-Nano 板上实测(2026-07-21,已编译待集成)**:Fun-ASR 仓库自带 llama.cpp fork
(`runtime/llama.cpp`,FetchContent pin llama.cpp 8086439a,板上须 ghfast 预克隆+
`FETCHCONTENT_SOURCE_DIR_LLAMA` 指过去;CUDA sm_87 -j4 编译 ~26min)。产出 4 个二进制在
`~/work/Fun-ASR/runtime/llama.cpp/build/bin/`,GGUF 在 `~/work/funasr-gguf/`(encoder-f16
447MB + qwen3-0.6b-q4km 484MB + fsmn-vad 1.7MB)。**坑:funasr-cli.cpp:191 硬编码
n_gpu_layers=0**,已 sed 成 99(GPU 才真用上)。实测:**每段边际 ~0.37s(GPU) vs 现役 qwen3
CPU ~1.5s,快 4 倍**,识别正确且自带标点;一次性载入 ~3.3s,CUDA compute buffer 1.3GB。
**已集成为离线引擎 `funasr`(2026-07-21),现场确认有提升 → 第一位/门面引擎**:给 fork 补了 serve 模式(`-a -`:stdin 一行
wav 路径→stdout 一行文本,模型常驻;补丁存 `voice/patches/funasr-cli-serve-gpu.patch`,含
ngl=99 + n_ctx/batch/ubatch 2048→1024/1024/512 瘦身:CUDA buffer 1319→299MB,峰值 RSS
2.5GB→1.63GB)。`OfflineFunAsr` 引擎:常驻子进程+select 超时防挂+载入探针(selftest 空文本
即 fail);**`unload_first=True`**——大宿主与旧引擎并存会击穿 8GB(载新前先卸旧;实锅:
qwen3+funasr 并存 → swap 抖动全板假死,救法 pkill -9 子进程)。板上验证:切换/selftest
0.933 带标点/退 DEBUG 还原子进程正确退出。**与 llama-server(VLM 3.4GB)不共存**,同
whisper/qwen3 约束。稳态 ~0.65s/句。注意 pgrep -f 会自匹配 bash 包装,判子进程用 ps -C。
- **FSMN-VAD 已接成第五 VAD 引擎 `fsmn`(2026-07-21)**:RTF 0.009、段起点自带 ~150ms
提前量(天然治掐头)。实现 = `llama-funasr-vad` 也补 serve 模式(路径进→"beg end"行+"."出),
`voice_vad.FsmnVad` 滚动缓冲 + 每 0.64s 批检 + 闭合判据(段尾距缓冲尾 <0.24s 视为未闭合,
留窗;flush 强制闭合)。threshold/min_silence 不外调(FSMN 自带状态机),min_speech/pre_roll
有效。板上验证:切换/selftest 0.933/还原/零进程残留。**坑:select+TextIOWrapper 多行响应
必死**——readline 把整包吸进 Python 缓冲,后续 select 看 fd 永远超时;多行协议必须二进制
os.read+自管缓冲(daemon `_dispose_vad` 收尸子进程,switch/restore/selftest 三处都调)。
fsmn(VAD)+funasr(离线) 组合 = llama-funasr-cli 伪流式管线的 daemon 原生复刻。
- **流式 zh 模型全是纯中文**,不认英文;**流式精度天生低于离线**(会重复字)。**中英混说该走离线 qwen3**。
- **现场实测(2026-07-21,MCP01 环境):xlarge 勉强能识别一些,zh-2025/multi-zh(300/200M)几乎全失败、不可用**。原因叠加:流式本就弱 + MCP01 丢音频 + 流式对丢块更敏感 + 用户反馈"识别一句后忽略几句"(间歇丢音频特征)。**结论:这套硬件上流式基本是对比工具,不是可用模式;可用路径是离线 qwen3**。换好麦后是否可用需重测。
- 流式切换**后台加载**(大模型同步会超 GUI HTTP 超时):HTTP 立即返回 `state:loading`,GUI `confirmStreamSwitch` 轮询 `/health.stream.loaded`。

**GUI 两级**:一级`识别模式`(VAD+离线/流式)→二级`模型`(随模式变)→参数随模式显隐(VAD 参数 vs 端点静音),增益全局。`voicelab.js` `fillModelSel/applyModeUI`。段级「🔁 重识」= `/asr_debug/seg_asr` 同段换引擎对比。

**xlarge 提速已做(2026-07-21,待现场验证)**:①**线程数反直觉**——板上实测 xlarge RTF:
1线程0.62 / **2线程0.56(最优)** / 3线程0.80 / 4线程0.72 / 6线程1.15,流式小块喂不饱多线程,
线程越多同步开销越大 → `from_transducer` num_threads 4→**2**。②**解码下专用线程**:原
`_feed_stream` 内联在采集循环(事件循环)跑,xlarge 单 320ms 块解码可达 ~0.3s → 卡循环 →
arecord 管道积压 → ALSA overrun 整段丢音 = "识别一句后忽略几句"的根因。现在采集侧只入队
(有界 32*320ms≈10s),`stream-decode` 线程取批合并喂(积压时追赶),结果 call_soon_threadsafe
甩回;worker 随引擎载/卸起停,`_set_stream_runtime` 先无条件停 worker 消除竞态。观测:
`/health.stream.backlog_s`(持续>0=跟不上实时)+`dropped`(涨=破上限丢块,该换小模型)。
③**真凶是端点检查时机**(现场 dropped=0 排除队列后锁定,codex 复核一致):`feed()` 原来
整批 decode 完才查一次 `is_endpoint`——端点在批中间时,批尾是语音则端点根本不触发(句子
粘连),触发了则 `reset` 把批内端点之后已解码的全清掉 → **整句丢**,批越大丢越多。修法:
端点检查移进 decode 循环内、每步一查,`feed()` 返回 `(partial, finals列表)`(一批可跨多句)。
板上验证:3句+静音拼一个大批喂 → 3 个 final 全找回(旧代码只出 1 句)。**现场复测
(2026-07-21,MCP01):四五句偶尔丢一句**——从"丢好几句"到基本可用;残余丢失疑似 MCP01
硬件丢音,换好麦后再判。④流式 transducer 无 LLM,同音字选择弱(在/再 之类语义错):
天生短板,greedy+无语言模型;要语义准确走离线 qwen3(LLM decoder)。进转写台/切模型的
流式载入均为后台任务+debug_tail ⏳/✅ 提示(/asr_debug 秒回,内联载会超 GUI 超时)。
**GPU 已否**(codex 复核):pip sherpa 是 CPU 版;k2-fsa 官方 cuda wheel(1.13.4+cuda cp310
aarch64)捆的是 ORT1.11/CUDA10.2/cuDNN8,**不适配 JetPack6**,只能按官方 Jetson 文档自编;
且 int8 算子可能回落 CPU 反复拷贝、统一内存与 VLM 互抢。RTF 0.56 已够实时(GR3D 才 6-10%
闲着也不缺算力),不值。板子已是 MAXN_SUPER。载入 24s 是一次性,另一回事。

**调研已否掉的**(codex+kimi,别重查):nemotron-3.5-asr 有中文但**只出 QNN/高通版**(Jetson 跑不了);nemotron-speech CPU 版是**纯英文**;Qwen3-ASR-1.7B 无 sherpa int8、CPU~4-5s、GPU 要 torch 服务抢显存 —— **都不值**。真瓶颈是音频 SNR,不是模型。相关:[[voice-frontend-s2]] [[voice-venv-dual-sherpa113]] [[board-memory-ceiling]]
