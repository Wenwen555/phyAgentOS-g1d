# 脚本入口

日常使用优先通过 `./scripts/core-env.sh paos agent` 进入 PAOS。

| 文件 | 用途 |
|---|---|
| core-env.sh | 选择项目 Core 环境，运行 PAOS/Python/Dora |
| project_cli.py | 项目实例配置、代理兼容及 Agent 接入 |
| init_instance.py | 初始化项目实例，供 onboard 调用 |
| prepare_dependency.py | 获取锁定的构建依赖 |
| build.py | 构建 Node 和 Skill 安装包 |
| node_entry.py | PyInstaller 的设备节点入口 |
| observe_once.py | 不经模型，单次调用观察 Query |
| run_arm_agent.py | 带肘部模式参数的 Agent 调试入口 |
| replay_arm_sequence.py | 工具序列回放验收；真实模式需要显式参数 |
| smoke_test.py | 独立模拟运行时验收 |

`diagnostics/` 保存非日常入口：

- check_model.py：模型接口诊断，会调用模型 API。
- check_agent_vision.py：Agent 图像理解诊断。
- check_agent_task.py：升降任务与验证链路诊断，真实模式可能执行动作。
- check_runtime_health.py：独立 g1d-observe 运行时健康采样。
- run_lift_once.py：升降柱单次操作诊断，默认预览，--execute 才执行。
- show_verifier_duplication.py：用 Core 原生代码复现验证请求重复发送执行记录的问题，
  只读取本地 AgentTask 记录，不调用模型、不连接机器人。
- evaluate_vision.py：在 09-11 真机抓取帧上评测视觉模型的识别能力，逐题独立调用并与人工
  核定的 ground truth 比对。会向模型端点发送图片；只读，不连接机器人。

运行诊断仍使用项目 Core 环境，例如：
```bash
./scripts/core-env.sh python scripts/diagnostics/check_agent_vision.py --help
```

已移除：reconcile_prompt_attempt.py（写死历史任务且包含回位动作）、
inspect_arm_runtime.py（临时 HTTP 查询，可用 paos skill status g1d-robot 替代）。
历史执行报告仍保留在 dist，运行中的 Skill 不受此次脚本整理影响。
