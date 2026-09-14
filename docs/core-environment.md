# Core 环境管理

Core 使用独立的 `environments/core/.venv`，设备构建与测试仍使用项目根目录 `.venv`。
两者共享本项目 `.python` 中的 CPython 3.12.14 解释器，各自拥有独立的依赖目录。
系统 Python 和 shell 启动配置不作修改。

## 环境文件

- `environments/core/pyproject.toml`：安装同级 `PhyAgentOS-core` 的可编辑源码。
- `environments/core/uv.lock`：完整依赖版本、来源及制品摘要。
- `environments/core/.python-version`：Python 3.12.14。
- `.tools/uv`：本地 uv 0.12.10，无须依赖 `/tmp` 中的工具。
- `scripts/core-env.sh`：指定 Core 环境运行命令、检查依赖或按锁恢复。

Core 源码修改会直接生效；本环境锁定依赖，不冻结同级 Git 仓库的源码。
源码基线与安装验证信息见 `environments/core/installation.json`。

## 日常使用

从 `PhyAgentOS-g1d` 目录执行，无须先激活虚拟环境：

```bash
./scripts/core-env.sh paos --help
./scripts/core-env.sh python --version
./scripts/core-env.sh check
```

本项目的 `paos` 入口会先调用 Core 的 `set_config_path`，固定选择
`.paos-instance/config.json`，并默认设置项目快照目录。所有项目操作使用此入口。
普通 `paos` 命令默认选择用户主目录配置；仅激活虚拟环境不会选择项目实例。

需要交互式 Python 时也可以在当前终端激活：

```bash
source environments/core/.venv/bin/activate
python --version
deactivate
```

## 恢复与更新

依赖恢复使用锁文件，不重新选择版本：

```bash
./scripts/core-env.sh sync
./scripts/core-env.sh check
```

有意更新 Core 的依赖声明或升级版本时，在 `environments/core` 目录使用
`../../.tools/uv lock`，审核 `uv.lock` 的变化后再执行上述 sync/check。
不要在 Core 环境中安装设备测试工具，也不要对根目录 `.venv` 手工安装 Core。

复制到新的 Linux x86_64 主机时，保留同级源码目录结构，重新创建环境，不复制 `.venv`。
准备 uv 0.12.10 到 `.tools/uv` 后，在项目根目录执行：

```bash
UV_PYTHON_INSTALL_DIR="$PWD/.python" .tools/uv python install 3.12.14
./scripts/core-env.sh sync
```

uv 原生管理的环境不要求安装 pip；依赖安装与检查使用 uv。

## 部署边界

项目实例现已初始化：`.paos-instance/config.json` 保存配置，`workspace/` 保存原生模板，
证据图像来源为 head、left_wrist、right_wrist，快照目录为 workspace/g1d_snapshots。
模型已配置为 `deepseek-flash`（主推理 / 图片理解 / 语义验证器 / 演化四角色相同），
Provider 为 `deepseek`，API 地址为 `https://api.deepseek.com`。
2026-09-14 之前主模型是 `deepseek/deepseek-v4-flash-vision-exp`；两者在该端点上都能读图，
`deepseek-flash` 的 reasoning token 更少、验证器响应更快，故统一到它。
模型能力实测与切换依据见 [模型选择与验证器可靠性](verifier-and-models.md)。
凭据仅保存在被忽略的实例配置中。
已通过 Core 原生 Provider 完成在线 API 验收：文本计算、合成图片左右颜色识别、
结构化工具调用及工具结果回传均成功。记录见 `dist/model-check-report.json`。
可用 `./scripts/core-env.sh python scripts/diagnostics/check_model.py` 重跑；这会调用付费模型 API，
仅发送合成输入，工具只返回本地测试值，不操作机器人。
当前 Core CLI 的单模型图片工具注册已通过项目入口补齐，继续复用原生 ImageTool 和
ProvidersManager；图片判断阶段（ImageTool 的 vision 请求）可单独指定模型，见
[Agent 图片工具接入](agent-vision.md)。

```bash
./scripts/core-env.sh paos status
./scripts/core-env.sh paos onboard
```

项目入口的 onboard 使用 Core 原生 Config/save_config/sync_workspace_templates，
重复运行保留配置和已有文件，只补齐缺失模板。Core 当前原始 onboard 使用默认主目录工作区，
因此由项目入口完成路径选择和初始化，不修改 Core 源码。

Dora CLI 与两个 Skill/Node 已完成项目级安装和 Core 托管 mock 验收，
日常启动和停止见 [部署说明](deployment.md)。模型基础调用和真机只读视觉诊断已通过，
真机运动任务仍待验收；见 [真机调试记录](real-robot-debug.md)。
