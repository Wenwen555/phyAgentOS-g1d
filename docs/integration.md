# G1-D 接入与复用边界

依据本地 Core 1.0.0、官方 Gateway 1.0.2 源码和已校验的 move-arm-by-ee 0.3.1 参考包。
本项目实现设备适配，不增加 Agent 或 HTTP 执行协议。

## 直接复用

| 能力 | 来源 |
| --- | --- |
| Agent、模型、图片工具 | Core agent/、providers/ |
| 平台配置、工作区 | Core config/ |
| HTTP 客户端、任务、证据、验证 | Core forge/、verification/ |
| Skill/Node 安装、摘要校验、Dora 生命周期 | Core skill_runtime/ |
| Skill 打包 | Core scripts/package_skill.py |
| HTTP API、invocation store、WebSocket | 官方 Gateway 制品 |
| Tool 协议、并发、身份和 Arrow 编解码 | 官方 ToolEndpointHandler、DoraToolEndpointBinding |
| 图像与本体消息 | forge_msgs.CompressedImage、JointState |
| DDS 与升降 RPC | 同级 Unitree SDK2 |

forge_tool 尚未独立分发，直接引用官方源码中的 .deps/forge-gateway/src，构建时打入节点；
不修改其协议。来源和摘要见 [第三方说明](third-party.md)。

## 观察

TeleImager 单帧 JPEG 进入有界最新帧缓存，节点输出 CompressedImage，由官方 Gateway
负责 /ws/images 的 Base64 JSON、序号和广播。
Snapshot Query 返回 `data.response.result.outputs.image_path`，供 Core 的 image 工具
以 vision 模式使用。PAOS_G1D_SNAPSHOT_DIR 必须指向 Agent 与节点均可读取的同一路径；
不同主机必须共享挂载，Core 不会自动下载机器人文件路径。

快照按内容摘要命名并保持不可变，避免改变此前 Query 的引用。快照目录不自动清理，
按部署的数据保留需求管理；AgentTask 证据仍由 Core 原生 retention 管理。

rt/lowstate 经与 SDK 示例一致的 CRC 检查，仅输出 G1-D 的 16 个有效腰部/双臂关节。
rt/hispeed_state 的 y 为柱高。Query 返回各路 DDS 接收时效与序号。
仅在数据新鲜且完整时向 Gateway 发布 JointState，断流后不重复发布旧状态伪造新鲜度。

## 升降

LiftAction 实现官方 start/cancel/status/result；实际控制只调用 AgvClient.HeightAdjust。
该接口接受归一化速度，目标高度由设备侧有时限闭环转换为速度，不提供通用速度透传。

- 显式启用并填写已确认行程后才能控制；读模式即使配置启用，也强制关闭控制客户端。
- 目标、容差、时限经过严格 Schema 和设备限制校验，并遵守 Gateway 的绝对 deadline。
- operation 的 max_concurrency=1，由官方 Handler/Gateway 执行并发约束。
- C++ 看门狗在命令更新中断约 250 ms、或高度反馈中断约 300 ms 后请求零速，
  实际响应受 SDK 通信与系统调度影响。进程强杀、主机断电或网络不可用时，软件不能保证送达。
- 正常到位、取消、超时和异常都请求零速并观测停止；要求不同序号的新反馈在设定窗口内稳定。
- stop_command_accepted 只表示 RPC 成功；stopped_observed 只表示柱高稳定观测，
  不等同于驱动器制动确认或独立硬件急停。
- 停止不能确认时返回 unknown，本进程拒绝再接纳升降。不得盲目重试或通过重启绕过未知状态。
- Gateway 注册确认丢失也请求停止；Runtime 关闭时等待设备侧取消与停止流程。

工作流遵循 primary activation → AgentTask → Query/Action → status/result → finalize，
任务目标由原生 verifier 判断，不增加 G1-D 专用 Core verifier。

## Bundle 和验证

两个 Skill 使用 manifest v2，包含 SKILL.md、mock/real profile、ToolSpec、Dora dataflow、
配置及原生库。Gateway 使用官方锁，G1-D 节点锁由真实归档计算。
artifacts.resolver=local 允许使用尚未上传 Registry 的节点；Node 归档仅含一个根目录可执行文件。

Dora 的双向 Tool 输入显式使用 256 条消息的队列。接纳、状态、结果与注册消息共用输入，
不能使用仅保留最新值的传输方式；否则紧接 Action 的状态轮询可能覆盖接纳响应并造成超时。
模拟运行验收保留了立即轮询的场景，以覆盖这一传输问题。

tests/test_gateway.py 使用真实 Gateway 和 Core 客户端，只替换设备为 mock。
scripts/smoke_test.py 用 Core 安装器、环境构建器验证后，通过 Dora 运行打包节点。
它不等于完整 AgentTask/LLM/真机任务验收。
