---
name: unit-tests-board
description: 板端纯逻辑单测在 tests/,Mac 上 uv run --with pytest --with numpy --with ruamel.yaml pytest tests/ -q 跑,无需 ROS/硬件;只测纯函数不测胶水
metadata:
  type: project
---

2026-07-20 建板端单测(经 Opus 子 agent 分析 + 评审筛选)。原则:**只测纯逻辑,
不测胶水**——运动学/协议/门控谓词测,串口 I/O、看门狗、线程生命周期不测(测的是
mock 自己);JS(ros.js)不测(为三行坐标变换引入 JS 测试链是负资产)。

- `tests/conftest.py`:stub rclpy/serial/zmq/cv2/primesense(rclpy.node.Node
  必须是真 class,MagicMock 实例不能当基类),sys.path 指向 board/home/jetson。
  **Mac 跑法:`uv run --with pytest --with numpy --with ruamel.yaml pytest tests/ -q`**
  (系统 python 无 numpy;board 端 Python 3.10,Mac 3.14,被测代码两边兼容)。
  **`--with ruamel.yaml` 不能漏**:漏了 `test_voice_brain.py` 9 条会以
  `ModuleNotFoundError` 全红,看起来像代码坏了。2026-07-28 就是这么误判过一次 ——
  以为有 16 条陈旧断言,其实 9 条是缺依赖、7 条才是真过期。
- `test_base_host.py`:solve 运动学(前进/横移/自转对称性 + 超速整体缩放不变量 +
  回归钉值)、raw_speed bit15 反向位、cksum 对 STS 数据手册 ping 包 FF FF 01 02 01 FB、
  base_blocked 优先级压制窗口(安全:手柄零速流必须压得住 LLM)。
- `test_ld19.py`:CRC 表已知前缀 + **板上抓的两个真实 47 字节包做 golden**(比合成
  包诚实)+ 篡改必拒;bin_points CW→CCW 方向翻转(镜像 bug 最难肉眼发现)、
  nearest-wins、conf/量程过滤。
- `test_cam_gates.py`:front_cam is_new_frame(**活样本:ts=None==None 哨兵碰撞
  会永久静默画面,分析时发现的真 bug,已修**)、depth LUT 锚点/clip/单调。

为可测顺手做的重构(好品味:抽纯函数,不引共享模块——为一个 `!=` 抽跨文件模块是
过度 DRY):base_host `base_blocked()`、ld19 `bin_points()`、front_cam
`is_new_frame()`;wrist/depth 的 seq 哨兵初值统一为 `last_pub_seq=0`(与 seq 同值=
无帧不发,消掉 -1 特殊态;wrist 那个曾是真崩溃:无帧时 encode None,NRestarts=5)。
23 用例全过,重构后板上五服务 + /scan /front_cam /wrist_cam 全部回归实测通过。

## 断言要断言命题,不要断言当时的值(2026-07-28 修完 7 条过期断言)

那 7 条钉的是「默认引擎叫 sensevoice」「label 是这串文案」「brain 有 kind 键」——
全是**当时的实现细节**,不是这条测试想守的命题,于是每次正常演进都红一片,
红久了整个套件就没人看了。修法按命题重写:
- 「换 tts 不碰 asr」→ 断言 asr **与输入相同**,不是等于某个引擎名。
- 「对齐时 drift 为空」→ applied **由 desired 推出来**,别写死引擎名。
- 「enums 是带元数据的对象」→ 断言 label **前缀**(后面挂着会随复测变的实测数字)。
- 例外:**默认值绊线**(`test_default_has_vad_and_audio_axes`)照抄字面值是对的 ——
  它的命题就是「默认被改了要有人知道」,改默认时必须同步改它并说明原因。
现在 **177 全过**。

相关:[[ros2-integration-plan]]、[[lekiwi-gui-tauri]]、[[vad-asr-tuning-results]]。
