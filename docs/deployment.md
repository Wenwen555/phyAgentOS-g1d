# 构建、安装与验收

从 PhyAgentOS-g1d 目录执行。代码构建和 mock 测试不会连接机器人。
后续已完成 g1d-observe/real 的只读验收，见 [真机调试记录](real-robot-debug.md)。
真机升降 profile 已在用户确认坐标与现场条件后启用，并完成一次上升 10 cm 的验收。

## 构建

```bash
python3 scripts/prepare_dependency.py
uv sync --frozen --all-groups --python 3.12
uv run python scripts/build.py
uv run pytest -q
uv run ruff check src scripts tests
```

configs/device.yaml 保存网卡、相机质量阈值和升降设置；configs/cameras.yaml 保存相机地址。
构建时生成 Bundle 内配置。节点 Python 3.12 是官方 forge_tool 源码的要求，Core 最低仍为 3.11。

## Gateway 与 Dora

本机已完成项目级安装：Dora 在 `.tools/dora`，项目入口自动将 `.tools` 加入子进程 PATH。
版本为 dora-cli 0.4.1 / dora-message 0.7.0。可以通过
`./scripts/core-env.sh dora --version` 检查；其中 Python 包显示 not found 不影响已冻结的节点。
Dora 原始归档 SHA-256 为
`f5518d49b7a466dae6dec22f0ce29a1a4d3ebab4114b251683ad5b1bf6332276`，安装二进制已与归档核对。

两个 Skill 已安装到 `.paos-instance/skills/`，共享的已校验 Gateway/G1-D 节点在
`.paos-instance/forge_runtime/nodes/`。Gateway 原始归档保存在
`dist/vendor/gateway-1.0.2.tar.gz`，运行不依赖 `/tmp` 文件。

正式实例已通过 Core 原生 start/status/stop 验收：观察查询、模拟升降、取消及前后证据均通过。
结果见 `dist/runtime-deployment-report.json`，Runtime 日志在 `.paos-instance/logs/skills/`。
验收结束时两个 Skill 均已停止；Dora 的共享 coordinator/daemon 保留运行。
未设置开机自启。后续 real 只读观察和 Agent 视觉诊断的结果见真机调试记录。

日常使用（无需重新安装）：

```bash
./scripts/core-env.sh paos skill list
./scripts/core-env.sh paos skill start g1d-observe --profile mock
./scripts/core-env.sh paos skill status g1d-observe
./scripts/core-env.sh paos skill stop g1d-observe
```

Core 会在需要时启动 Dora 基础进程。停止 Skill 会关闭其节点，不关闭共享 Dora 服务。

官方 Gateway 归档：
<https://github.com/Forgelab-Robotics/adapter-forge-gateway/releases/download/v1.0.2/forge-gateway-v1.0.2-ubuntu20.04-x86_64.tar.gz>

SHA-256：`07584846f16012c6137b10cf743073cd2fe9a7f64564ba2322a7090e66fd3076`。
也可通过 Core RegistryClient.node / DownloadCache 获取，无需自行编译 Gateway。
Dora CLI 必须为 0.4.1 / dora-message 0.7.0，安装方法见 [Core README](../../PhyAgentOS-core/README_zh.md)。

## 仅模拟验收

```bash
uv run python scripts/smoke_test.py --gateway-archive /absolute/path/to/gateway.tar.gz --dora /absolute/path/to/dora
```

脚本只允许 mock，临时安装 Bundle/Node，使用 Core 校验器和环境构建器，再执行 dora run。
检查状态、三路快照、Action 终态、取消和 WebSocket 前后证据。结束后关闭测试进程并删除
临时实例；日志 build/smoke-test.log，结果 dist/smoke-report.json。19002 被占用时拒绝运行。

## Core 原生安装流程

按 Core README 在 Python 3.12 环境安装同级 PhyAgentOS-core，得到 paos 命令。
本机已完成此步，环境为 `environments/core/.venv`，详见
[Core 环境管理](core-environment.md)。下方命令均通过
项目入口 `./scripts/core-env.sh paos`，以固定选择 G1-D 实例。
按原生配置流程设置实例、工作区与 Provider。configs/paos.example.json 仅给出工作区和
图像来源，没有模型凭据，不是完整部署配置。

以下示例只启动 mock：

```bash
export PAOS_G1D_SNAPSHOT_DIR=/absolute/path/to/PhyAgentOS-g1d/workspace/g1d_snapshots
./scripts/core-env.sh paos skill install dist/skills/g1d-observe-0.1.0.tar.gz --local
./scripts/core-env.sh paos forge-node install g1d-observe gateway --archive /absolute/path/to/gateway.tar.gz
./scripts/core-env.sh paos forge-node install g1d-observe g1d --archive dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz
./scripts/core-env.sh paos forge-node verify g1d-observe gateway
./scripts/core-env.sh paos forge-node verify g1d-observe g1d
./scripts/core-env.sh paos skill inspect g1d-observe
./scripts/core-env.sh paos skill start g1d-observe --profile mock
./scripts/core-env.sh paos skill status g1d-observe
./scripts/core-env.sh paos skill stop g1d-observe
```

升降模拟将 Skill 与 Bundle 名改为 g1d-lift，使用同一套 Node 归档。仅保留一个活动 Runtime，
切换遵循 Core 的 skill switch 约束。不要同时运行 vendor 示例。

## 真机 profile 的前提

观察：确认网卡与 TeleImager 配置，选择 g1d-observe 的 real profile。
它只创建 DDS/JPEG 订阅，不初始化升降控制客户端。图像分析需要已配置的多模态 Provider，
快照目录需同时对 Agent 与节点可读。

升降：先确认柱高坐标零点和最小/最大机械行程，写入 configs/device.yaml，再将
limits_confirmed 和 enabled 设为 true，重新构建并通过 Core 安装。
本机当前两者为 true，行程为 0–0.42 m；这是本台设备经现场确认的配置，不能直接推广到其他机器。
最大归一化速度指令 0.15，最长动作时间 30 秒。g1d-observe 即使共用配置也会在节点入口
强制禁用升降，不创建控制客户端。

仅修改配置、Skill manifest 不变时，当前 Core CLI 会因 manifest 相等而显示 already ready，
不替换 Bundle 内容。此次复用 Core 的 SkillInstaller.install，传入实际归档 SHA-256，
完成原生校验、活动 Runtime 检查与旧 Bundle 备份，并核对安装后的配置与源配置一致。
没有修改 Core 或直接改写已安装 Bundle。

第一次真实运动应由现场人员核实环境、停止手段和机械行程，单独完成有界目标验收。
启动 Runtime 本身不会主动升降，动作必须由显式 Action 触发。更多边界见 [接入说明](integration.md)。
