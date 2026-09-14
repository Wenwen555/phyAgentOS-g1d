# Agent 图片工具接入

本地 Core CLI 创建单个 LLMProvider，但 AgentLoop 仅在 provider 为 ProvidersManager 时
注册 ImageTool。本项目在启动入口中选择 G1dAgentLoop 子类，先完成 Core 原生工具注册，
再为缺失的 image 工具补充注册。原有主模型、Forge 工具、任务和验证流程继续使用 Core。

`src/paos_g1d/agent_integration.py` 将现有 Provider 交给原生 ProvidersManager，再创建原生
ImageTool。`main` 模式保持 Agent 自己的 Provider；`multimodal` 模式是 ImageTool 的 vision 请求
实际使用的模型，即图片判断阶段，可用 `agents.modes.models.multimodal.model` 单独指定（未配置时
或与 `agents.defaults.model` 相同时回退到 Agent 自己的 Provider）。不复制图片编码、HTTP 请求或
模型响应处理代码。

选这个模型有一条硬要求：**它必须真的接收 `image_url` 内容**。模型名可用不代表能看图，
而且指错模型不会报错：vision 请求照样成功返回一段"我没收到图像"的文字，Agent 会据此
判定视觉不可用。

| 日期 | 模型 | 实测（320×160 合成图，白字 `VISION-OK-7391`） |
| --- | --- | --- |
| 2026-09-11 | `deepseek-flash` | 6/6 回「没有图片」→ 当时判定不能读图 |
| 2026-09-11 | `deepseek-v4-flash-vision-exp` | 正确读出文字 → 当时唯一的可用视觉模型 |
| 2026-09-14 | `deepseek-flash` | **行为已变**：同一测法 **3/3 正确读出** |
| 2026-09-14 | `deepseek-v4-pro` | 仍不能读图：**0/3** |

所以 `deepseek-flash` **现在可以读图**，`deepseek-v4-pro` 仍然不行。判断一个模型是否真的
收到图片，看 **`prompt_tokens`** 比看回答措辞可靠：带同一张图，能读图的模型是 247，
收不到图片的是 103（图被端点丢弃）。

换模型后必须先用一张已知内容的图复验它能否读出内容，再看真机画面。
详见 [真机调试记录](real-robot-debug.md) 与 [模型选择与验证器可靠性](verifier-and-models.md)。

`scripts/project_cli.py` 在 agent / gateway 命令进入 Core CLI 前安装这项接入。
实现方式是仅在当前项目进程中替换 Core 模块的 AgentLoop 类引用；磁盘上的 Core 源码不变。
如果 Core 已注册 image 工具则不重复注册。图片判断阶段可单独选模型，但只覆盖 ImageTool 的 vision
请求；主 Agent 的推理、工具调用仍使用 `agents.defaults.model`。

## 使用

从项目根目录执行，启动 mock 后可与 Agent 交互：

```bash
./scripts/core-env.sh paos skill start g1d-observe --profile mock
./scripts/core-env.sh paos agent
```

例如询问：“请获取 head 相机的当前快照，用 image 的 vision 模式描述画面。”
完成后停止模拟节点：

```bash
./scripts/core-env.sh paos skill stop g1d-observe
```

普通环境中的裸 paos 命令不会自动选择此项目入口，也不会加载这项接入。
这里只配置了图片理解；ImageTool 原有的生成图片模式仍需要它自己的服务配置。

## 验证

离线回归在独立 Core 环境运行，不向模型发送请求：

```bash
./scripts/core-env.sh python -m unittest discover -s tests_core
```

在线诊断需已运行 g1d-observe/mock：

```bash
./scripts/core-env.sh python scripts/diagnostics/check_agent_vision.py
```

脚本使用 Core 原生 AgentLoop、动态 Forge 客户端、Query 工具和 ImageTool，
让 Agent 自己调用 context → camera_snapshot → image，再回答。
它只向模型开放这三个观察工具，并检查 Runtime profile 与实际状态的 simulated 标记。
测试发送模拟图片并产生模型 API 用量，不调用机器人动作。
它属于无 AgentTask 的诊断 Query，不能当作完整任务 Verifier 或真机验收。

结果保存在 dist/agent-vision-report.json，包含 Agent 回答、工具调用记录和图像识别检查。
integration_passed 表示调用链成功；visual_checks 单独记录背景颜色和小字号文字准确性。
首次测试识别出灰蓝色背景和 head，但将 SIMULATION 误读为 SILICON，不能把链路成功
等同于 OCR 总是准确。不要依赖微小文字或一次模型判断直接决定物理动作。

本轮还观察到一个 Runtime 问题：问答完成后，持续运行的 mock 设备节点丢失 Endpoint 注册，
Gateway 仍在线但工具变为 not ready；停止时 Dora 最终强制结束了未响应的 g1d 节点。
当时该问题尚未定位，首次结果仅证明一次问答链路成功；后续处理见下文。

后续更新（2026-09-08）：真机调试复现并捕获了 recv_async 阻塞，已在设备节点改用
工作线程执行带超时的同步接收。mock / real 分别通过 120 秒持续检查及正常停止，
三路真实图像的 Agent 问答也已完成。详见 [真机调试记录](real-robot-debug.md)。
上方保留的是首次 mock 测试的历史现象。

真机只读诊断使用 `scripts/diagnostics/check_agent_vision.py --profile real`，要求先启动
`g1d-observe --profile real`。这会将三路真实快照发送给配置的模型服务；仅开放观察工具。
