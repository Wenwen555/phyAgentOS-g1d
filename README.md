# PhyAgentOS G1-D 本地集成

本项目补充 G1-D 设备节点与原生 Skill Bundle。Agent、模型 Provider、图片理解、任务管理、
证据、验证、Runtime 管理全部复用同级 `../PhyAgentOS-core`；Gateway 与 Tool 协议库复用
官方 Forge Gateway 1.0.2。没有修改 Core 或提交 PR。

```text
Core Agent / AgentTask → ForgeToolClient → 官方 Gateway
                                          ↓ Dora / forge_tool
                                    G1-D 设备节点
                                    ├── TeleImager JPEG
                                    └── Unitree SDK2 / DDS
```

## 已实现范围

| Tool | 语义 | 能力 |
| --- | --- | --- |
| `g1d.state` | Query | 柱高、16 个有效腰部/双臂关节位置与速度，含数据时效 |
| `g1d.camera_snapshot` | Query | 三路 JPEG，检查尺寸、过暗和时效，保存本机图片供现有 image 工具读取 |
| `g1d.set_height` | Action | 升降柱绝对高度闭环，含范围、时限、取消、反馈中断处理和停止观测 |
| `g1d.arm_state` | Query | 双臂及内部 Dex1 实测角度、参考、阶段与反馈时效 |
| `g1d.move_joints` | Action | 指定关节绝对角度和时长，持续保持、状态/结果/取消 |
| `g1d.perception_state` | Query | 读取持续感知的最新一帧，以及可选的最近数秒历史（按接收时间倒序，年龄在读取时刻重算） |
| `g1d.perception_session` | Action | 限时持续抓取原生相机 JPEG 供 Agent 视觉模型查看（≤300 s）；含时限、取消、进度事件 |

- `g1d-observe` 只注册两个 Query，启动和关闭均不发送运动命令。
- `g1d-lift` 增加升降 Action。
- `g1d-robot` 增加参数化机械臂及内部 Dex1 Action；底盘控制尚未实现。见 [参数化机械臂接入](docs/parameterized-arm.md)。
- 原始 prompt、参考回放和原生 Agent 入口已提供；mock 工具回放与模型执行分别记录，尚未真机验收此新接口。
- 视觉放置触发闭合（“检测到放好就自行闭合”）是 Agent 轮询 `g1d.perception_state` 并自行判断的闭环，不是自动触发器；网关没有 session 语义和条件原语，`grasp_target` 也不做图像检查。0.1.16 起两路各判一半，**物体认腕部、手指认头部**：抬臂姿态下物体在头部画面里可能太小或太淡、不一定分辨得出，所以物品是不是要抓的那个、有没有到夹爪口，由腕部近距离画面判定；手指是否张开、指间有没有东西、附近有没有人手，由头部画面判定（腕部在抬臂姿态下通常拍不到手指）。0.1.17 起两半在**同一次读取**里同时成立就自行闭合，不再要求连续两轮（操作员要求一次即可触发）。闭合边闭边看编码器，手指被物品挡住即停在该位置并在停位保持（`contact_detected` + `blockage_rad`，该角度即本次确认的夹持角度，下次命令前不变），闭合后再用同一分工复核夹住的物品；放开用 `release`，它会先开到位再自动闭回空夹静止位，不会把夹爪留在张开状态。开爪目标与空夹静止位来自 `configs/device.yaml` 中经实测确认的 Dex1 行程，不再是 vendor 映射常量 5.4。工作流与安全约束见 [SKILL.md](skills/g1d-robot/SKILL.md) 与 [docs/grasp.md](docs/grasp.md)。
- 持续感知只抓取原生 JPEG 并落盘，供 Agent 已有的 image 工具用视觉模型查看；本机没有检测服务、
  深度源与相机标定，因此不提供目标检测、跟踪、深度或三维坐标，图像像素不是机器人目标坐标。
  官方 Forge Gateway 1.0.2 尚不支持 session 语义，故持续感知以限时 Action 注册，读结果用独立 Query。
  状态存储只保留每路最新 1 帧（0.1.13 起取消 5 秒环形缓冲与 `history_s`/`max_history` 参数，
  每次读到的就是当时最新的一帧）；要看过程就按 `interval_s` 反复读取并比较自己看过的帧，
  静帧不是相机原生帧率的视频，不能据此推断连续轨迹或速度。
- 每个 Skill 都有 `mock` / `real` profile。mock 只生成模拟状态和标有 SIMULATION 的图片，
  不加载 SDK、不连接机器人。配置模型默认禁用运动；本机经用户确认后，
  已启用真实升降，柱高坐标范围为 0–0.42 m，向上为正、最低位置为零。

## 文件

```text
configs/                 # 电脑与设备配置；修改不会更改系统网卡
src/paos_g1d/
  contracts.py           # 设备配置、输入输出 Schema
  cameras.py             # TeleImager 取流、最新帧、快照
  observe.py             # 单次图像与关节状态观察包
  device.py              # SDK C ABI 接入与独立 mock
  endpoints.py           # 设备查询、升降 Action 与公共生命周期
  manipulation/          # 机械臂与 Dex1 操作
    __init__.py
    arm.py               # 关节参数、范围、Native/Mock 后端
    arm_endpoint.py      # 机械臂 Query/Action 生命周期与到位检查
    elbow_frames.py      # 肘部相对上臂角度与地面仰角转换
    arm_recipe.py        # 参考动作序列与逆向恢复
    grasp_action.py      # 所选侧 Dex1 直接打开、闭合与保持
    grasp_verifier.py    # 已停用，保留原视觉验证实现供参考
  perception/            # 持续感知：原始图像供 Agent 视觉模型查看，无检测/三维定位
    __init__.py
    contracts.py         # 持续感知请求/输出 Schema
    state_store.py       # 最新感知结果与有效性
    session_endpoint.py  # 持续感知 Action 与最新状态 Query
  node.py                # Dora 节点，复用官方 Handler / Binding
native/                  # C++17 DDS 订阅、升降 RPC、命令超时看门狗
skills/g1d-observe/       # skill.yaml、SKILL.md、profiles、assets
skills/g1d-lift/
skills/g1d-robot/         # 当前综合包：观察与机械臂控制
scripts/                 # 环境、构建、当前操作入口和验收
scripts/diagnostics/     # 模型、视觉、升降柱等专项诊断
tests/                   # 设备边界和 Gateway/Core 互通测试（根 .venv）
tests_core/              # Core 侧 Agent、图片与验证不变量测试（Core 环境）
environments/core/       # Core 运行时环境：pyproject、uv.lock、安装基线（.venv 忽略）
docs/                    # 部署、契约边界、依赖来源
dist/                    # 生成的节点归档、Skill 归档、验证报告（忽略）
```

## 依赖

本项目只提供 G1-D 设备节点与 Skill Bundle。Agent、模型 Provider、图片理解、任务与语义验证、
Skill/Runtime 安装与状态全部来自同级 `PhyAgentOS-core`；真机通信的 C++ SDK 来自同级
`unitree_sdk2-main`。两者都以**同级检出**使用：不复制、不修改上游源码，不提交 PR，
也不是本仓库的子模块。

```text
unitree-g1d/                  # 工作区父目录（不是本仓库）
├── PhyAgentOS-core/          # 必需：Agent / Provider / 验证 / Runtime
├── unitree_sdk2-main/        # 必需：Unitree SDK2（编译 native/、运行 real profile）
└── PhyAgentOS-g1d/           # 本仓库
```

### PhyAgentOS Core（同级 `../PhyAgentOS-core`）

| 项 | 值 |
| --- | --- |
| 来源 | 同级检出 `../PhyAgentOS-core`，`https://github.com/PhyAgentOS/PhyAgentOS-core.git`，MIT |
| 包名 / 版本 | `PhyAgentOS-ai` 1.0.0 |
| 安装方式 | 可编辑源码安装；唯一声明在 `environments/core/pyproject.toml` 的 `[tool.uv.sources]`（路径 `../../../PhyAgentOS-core`） |
| 运行环境 | 独立 `environments/core/.venv`（CPython 3.12.14），与设备构建/测试用的根目录 `.venv` 分离 |
| 验证基线 | commit `b29744a0a72cdb41462c36fe077b6788bb1b35c9`，记录在 `environments/core/installation.json` |
| 按锁恢复 | `./scripts/core-env.sh sync && ./scripts/core-env.sh check` |

按使用阶段划分，Core 的依赖面如下。**设备节点本身不导入 Core**，只使用 Forge 协议库
`forge_tool`；Core 出现在 Agent、配置、构建与测试路径上。

| 阶段 | 本项目入口 | 用到的 Core 模块 |
| --- | --- | --- |
| 设备节点运行 | `src/paos_g1d/node.py`、`scripts/node_entry.py` | 无（仅 `forge_tool`，见下文 Forge Gateway） |
| Agent 与验证 | `src/paos_g1d/agent_integration.py`、`src/paos_g1d/verification.py`、`scripts/run_arm_agent.py`、`scripts/diagnostics/check_agent_*.py` | `agent.loop`、`agent.session_verifier`、`agent.tools.image.ImageTool`、`providers.providers_manager`、`providers.litellm_provider`、`verification.request_builder`、`bus.queue` |
| 配置 / 实例 / 运行时 | `scripts/project_cli.py`、`scripts/init_instance.py`、`scripts/observe_once.py`、`scripts/replay_arm_sequence.py`、`scripts/diagnostics/*` | `config.loader`、`config.schema`、`cli.commands`、`utils.helpers`、`forge.tool_client`、`forge.task`、`forge.observation`、`skill_runtime.manager`、`skill_runtime.state` |
| 构建与打包 | `scripts/build.py` | Core 的 `scripts/package_skill.py` 打包器、`skill_runtime.manifest.load_manifest` 校验器 |
| 测试 | `scripts/smoke_test.py`、`tests/`、`tests_core/` | `skill_runtime.installer`、`skill_runtime.archive`、`skill_runtime.state`、`forge.tool_client`、`forge.observation`、`verification.request_builder`、`verification.contracts` |

- 根目录 `.venv` **不安装** Core：`pyproject.toml` 的 `pythonpath` 直接把 `../PhyAgentOS-core`
  加入导入路径，因此改 Core 源码立即生效，但依赖由根 `.venv` 自己的 `uv.lock` 固定。
- `tests_core/` 不随 `pytest` 默认收集，固定用 Core 环境运行：
  `./scripts/core-env.sh python -m unittest discover -s tests_core`。
- 对 Core 行为的两处调整都放在本项目侧子类里，Core 源码不动：
  `src/paos_g1d/agent_integration.py`（图片工具注册、可单独指定图片判断模型）与
  `src/paos_g1d/verification.py`（覆写 `_build_request` 对执行记录做投影）。
- 复制到新主机时保留上面的同级目录结构，重建环境，不复制任何 `.venv`；步骤见
  [Core 环境管理](docs/core-environment.md)。

### Unitree SDK2（同级 `../unitree_sdk2-main`）

| 项 | 值 |
| --- | --- |
| 来源 | 同级检出 `../unitree_sdk2-main`，`project(unitree_sdk2 VERSION 2.0.0)` |
| 许可 | SDK `LICENSE`，以及随附的 CycloneDDS / CycloneDDS-CXX / iceoryx / RapidJSON 许可证 |
| 接入方式 | `native/CMakeLists.txt` 用 `UNITREE_SDK_ROOT`（默认 `../../unitree_sdk2-main`）`add_subdirectory`，并链接目标 `unitree_sdk2` |
| 需要的产物 | `lib/<arch>/libunitree_sdk2.a`（IMPORTED STATIC；目标平台为 `lib/x86_64/`）与 `thirdparty/lib/x86_64/libddsc.so.0`、`libddscxx.so.0` |
| 使用的头文件 | `unitree/robot/channel/channel_publisher.hpp`、`channel_subscriber.hpp`、`unitree/idl/hg/{LowCmd_,LowState_,IMUState_}.hpp`、`unitree/idl/ros2/Point32_.hpp`、`unitree/robot/g1/agv/g1_agv_client.hpp` |
| 打包 | `scripts/build.py` 把两个 DDS 动态库复制进 `skills/*/assets/native/`，并把 SDK 与第三方许可证放进 Bundle；生成的 `.so` 不入库 |

- 只有 `real` profile 加载 SDK（`--backend unitree --device device.yaml --sdk-library .../libg1d_sdk.so`）；
  `mock` profile 用 `--backend mock`，不链接 SDK、不连接机器人，配置默认禁用运动。
- `native/` 只编译本项目自己的 `g1d_sdk.cpp`、`joint_motion.hpp`、`sdk_joint_driver.hpp`
  与单元测试，不复制 SDK 示例程序，也不修改 SDK 源码。
- 目标平台是 Linux x86_64；`scripts/build.py` 在其他平台上直接拒绝构建。

### 其他固定依赖

| 依赖 | 版本 / 摘要 | 来源与用途 |
| --- | --- | --- |
| Forge Gateway 源码 | 1.0.2，归档 SHA-256 `8fbd3f67…705322a` | `scripts/prepare_dependency.py` 按摘要下载到被忽略的 `.deps/forge-gateway/`（Apache-2.0）；`forge_tool` 尚未单独分发，设备节点直接使用该源码，不修改协议代码 |
| Gateway 可执行制品 | `gateway-1.0.2-linux-x86_64-tar-gz`，SHA-256 `07584846…66fd3076` | Core Resource Registry 的 `move-arm-by-ee 0.3.1` Bundle 锁；写入 `dist` Bundle 的节点清单 |
| Dora / dora-message | 0.4.1 / 0.7.0 | PyPI，见 `uv.lock`；节点数据流运行时 |
| forge-msgs | 1.0.1 | PyPI；沿用 Gateway 的 Arrow `CompressedImage` / `JointState` 格式 |
| Python | 3.12（3.12.14） | 根 `.venv` 见 `uv.lock`，Core 环境见 `environments/core/.python-version` |

`PhyAgentOS-core` 与 `unitree_sdk2-main` 不会自动获取：新主机上先按
[依赖来源](docs/third-party.md) 放置这两个同级目录，再执行下面的构建流程。

## 构建与测试

启动命令速查（相机、机械臂、夹爪、视觉触发闭合、升降、排错）见 `~/project/quickstart.md`
（仓库外，本机路径 `/home/robot/project/quickstart.md`）。
人工对准物品后的左右 Dex1 抓夹流程与原生 PAOS prompt 示例见 [夹爪操作](docs/grasp.md)。

目标为 Linux x86_64、Python 3.12、Dora 0.4.1 / dora-message 0.7.0。
从本目录执行，需要 uv、CMake、C++ 编译器和同级 `PhyAgentOS-core`、`unitree_sdk2-main`
（版本、安装方式与依赖面见上节 [依赖](#依赖)）：

```bash
python3 scripts/prepare_dependency.py
uv sync --frozen --all-groups --python 3.12
uv run python scripts/build.py
uv run pytest -q
uv run ruff check src scripts tests
```

依赖准备脚本只获取摘要固定的官方源码，供节点直接导入 forge_tool，不启动服务。
构建脚本编译设备库与独立节点，计算真实节点锁，再调用 Core 的原生打包与校验代码，
不安装 Skill 或连接机器人。设备代码未修改时可用 `--skip-binary` 重新生成配置。

输出为 `dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz`、
`dist/skills/g1d-observe-0.1.0.tar.gz`、`dist/skills/g1d-lift-0.1.0.tar.gz`；
真实摘要与大小记录在 `dist/build-report.json`。

本次验证：16 项自动化测试通过；打包节点通过官方 Gateway + Dora 的 mock 联调，
覆盖状态、三路快照、升降到位、取消，以及动作前后的图像/状态证据。
对应模拟制品摘要和结果保存在 `dist/smoke-report.json`。

configs 为配置源，Bundle 内副本由构建生成。network.yaml 与 device.yaml 的网卡名称必须
一致。修改配置后重新构建并通过 Core 安装，不修改已安装的 Bundle。当前为本地开发版本，
构建会覆盖本地 dist；对外发行必须递增版本。

安装、模拟验收和真机启用见 [部署说明](docs/deployment.md)。
Core 已安装在独立的 `environments/core/.venv`，使用和依赖恢复见
[Core 环境管理](docs/core-environment.md)。根目录 `.venv` 用于设备构建与测试。
项目入口已补齐单模型 Agent 的图片工具注册，使用和验收见 [Agent 图片工具接入](docs/agent-vision.md)。
接口约定见 [接入说明](docs/integration.md)，来源见 [第三方说明](docs/third-party.md)。
代码框架（分层、Tool 路由、模块依赖、端到端时序、构建流水线）见
[架构图](docs/architecture.md)；Skill 的行为规则只写当前状态，历史见
[Skill 版本演进](docs/skill-changelog.md)。
可切换的模型、各自的实测视觉能力，以及验证器在 `max_tokens` 上限下的失效模式见
[模型选择与验证器可靠性](docs/verifier-and-models.md)。
Core 把同一批执行记录重复三次的问题（三个键占验证请求 context 的 77%，其中纯重复 50.5%）见
[待提交的 Core issue](docs/issues/README.md)；
本项目侧已覆写 `_build_request` 做投影，实测 `prompt_tokens` 减少 46%。
DeepSeek 已完成真实下降 5 cm 的规划、原生工具选择和 enforce 模式 AgentTask 验收，
步骤、范围与完整记录见 [AgentTask 升降验收](docs/agent-task.md)。
`--prompt` 原样接收自然语言，不检查句式或预先解析位移；模型自行读取柱高、
计算目标和生成验收条款，工作流规范来自安装的 g1d-lift Skill。
此前“上升 5 cm”已完成 mock 和真机验收；本次移除句式检查仅作离线测试，未再次运动。

## 设备记录与限制

- USB 网卡 `enx9c69d36fbc0b`，电脑 `192.168.123.99/24`。
- TeleImager `192.168.123.164`，网页端口 `60001/60002/60003`，JPEG ZMQ 端口
  `55555/55556/55557`。头部 1280×480 双目，腕部各 640×480。
- 头部为 1280×480 拼接双目图（并排两个 640×480 眼）。右半曾经模糊的设备问题已修复，
  2026-09-10 起的实拍帧两半清晰度相当；09-08 及更早的帧仍带该缺陷，不要用作清晰度基准。
- 柱高来自 rt/hispeed_state 的 Point32.y，不是整机高度或 TCP 世界坐标。
- 原始 JPEG 没有采集时间戳；Query 与 Gateway 使用本机接收时效。
- VLM 复用 Core 多模态 Provider，需要另行配置模型和凭据，没有新增 VLM 服务。
- 已完成真机只读状态、三路图像和 Agent 视觉问答，以及 mock / real 各 120 秒运行检查。
  已通过 Core Tool API 完成一次真实上升 10 cm 的有界验收：实际 98.13 mm，
  在 2 mm 目标误差内，停止反馈已确认，运动 Runtime 已关闭。
  已实测内部 Dex1 行程，开爪目标与接触阈值写入 configs/device.yaml，并在真机上核对包络拒绝、
  开爪到位与空夹闭合判定；左手修复后左右开爪实测 5.1933 / 5.1852 rad、空夹闭合均判 `fully_closed`。
  0.1.12 增加角度管理：夹住时保持在测得停位（`blockage_rad` 即本次确认的夹持角度），
  放开用 `operation:"release"`——开到目标后自动闭回空夹静止位，夹爪不会留在张开状态。
  夹住物体的接触判定仍需操作员放置物体时复测。
  详情见 [真机调试记录](docs/real-robot-debug.md)。

同级 ../configs 保留为历史记录，后续以本目录配置为准，两处不自动同步。
