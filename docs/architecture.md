# G1-D 接入代码框架

本文用 Mermaid 描述 `PhyAgentOS-g1d` 的分层、路由、模块依赖、端点生命周期与构建流程。
图依据当前源码绘制（`src/paos_g1d`、`native`、`skills`、`scripts/build.py`）。

## 1. 分层总览

```mermaid
flowchart TB
    subgraph L1["编排层 · ../PhyAgentOS-core（不修改）"]
        AG["Agent loop / 模型 Provider / image 工具"]
        TASK["AgentTask / 证据 / 验证"]
        CLI["paos CLI / Skill 安装器"]
    end

    subgraph L2["协议层 · 官方 Forge Gateway 1.0.2（不修改）"]
        HTTP["HTTP API + invocation store"]
        ROUTE["Tool 路由 / 并发 / deadline"]
        BIND["DoraToolEndpointBinding"]
    end

    subgraph L3["设备适配层 · src/paos_g1d（本仓库）"]
        NODE["node.py<br/>端点注册 + tick 循环"]
        EP["endpoints.py<br/>Query / Action 生命周期"]
        MAN["manipulation/<br/>机械臂与 Dex1"]
        PER["perception/<br/>持续感知"]
        CAM["cameras.py<br/>取流与内容寻址快照"]
        OBS["observe.py<br/>图像+臂态观察包"]
    end

    subgraph L4["原生驱动层 · native/（C++17）"]
        SDK["g1d_sdk.cpp<br/>DDS 订阅 + AgvClient RPC"]
        JOINT["sdk_joint_driver.hpp<br/>LowCmd 5 ms 发布"]
        WD["看门狗<br/>命令 250 ms / 反馈 300 ms"]
    end

    HW["G1-D 真机 + TeleImager"]
    MOCK["MockDevice / MockArm / 模拟相机"]

    CLI --> TASK --> AG
    AG -->|"ForgeToolClient"| HTTP
    HTTP --> ROUTE --> BIND
    BIND <-->|"Dora tool 消息"| NODE
    NODE --> EP
    NODE --> MAN
    NODE --> PER
    NODE --> CAM
    NODE --> OBS
    EP --> SDK
    MAN --> SDK
    SDK --> JOINT
    SDK -.- WD
    SDK <-->|"DDS / RPC"| HW
    CAM <-->|"ZMQ JPEG"| HW
    NODE -. "profile 切换为 --backend mock" .-> MOCK
```

## 2. Dora 数据流与 Tool 路由

`scripts/build.py` 的 `dataflow()` 为每个 profile 生成这张图。工具路由是**双向成对**的：
`g1d/<route>_tool_out → gateway/<route>_tool_in`，以及反向的
`gateway/<route>_tool_out → g1d/<route>_tool_in`。

```mermaid
flowchart LR
    AG["Core Agent / ForgeToolClient"]

    subgraph GBOX["gateway 节点"]
        GW["tool 路由<br/>request_input_id: tool_request<br/>response_output_id: tool_response<br/>invoke_timeout_ms: 330000"]
    end

    subgraph NBOX["g1d_node"]
        ND["Endpoints.bindings<br/>route → ToolEndpointHandler<br/>+ DoraToolEndpointBinding"]
    end

    AG -->|"HTTP invoke / status / result"| GW
    GW -->|"route_tool_out"| ND
    ND -->|"route_tool_out（queue_size 256）"| GW

    ND -->|"state"| GW
    ND -->|"head / left_wrist / right_wrist"| GW

    ND -.-> ROUTE["route 清单<br/>state · camera · observe<br/>perception_state · perception_session<br/>arm_state · arm · grasp · lift"]
```

> 队列必须显式设为 256 条：接纳、状态、结果与注册消息共用同一路输入，Dora 默认的
> “只保留最新值”会吞掉 Action 接纳响应，导致紧随其后的状态轮询超时。

## 3. Python 模块依赖

```mermaid
flowchart TD
    node["node.py"]
    contracts["contracts.py"]
    device["device.py"]
    cameras["cameras.py"]
    endpoints["endpoints.py"]
    observe["observe.py"]
    arm["manipulation/arm.py"]
    elbow["manipulation/elbow_frames.py"]
    armep["manipulation/arm_endpoint.py"]
    grasp["manipulation/grasp_action.py"]
    perssess["perception/session_endpoint.py"]
    store["perception/state_store.py"]
    percon["perception/contracts.py"]
    verify["verification.py"]
    agentint["agent_integration.py"]

    node --> device
    node --> cameras
    node --> endpoints
    node --> observe
    node --> contracts
    node --> arm
    node --> armep
    node --> grasp
    node --> perssess
    node --> store

    endpoints --> contracts
    observe --> endpoints
    observe --> contracts
    observe --> arm
    arm --> contracts
    arm --> elbow
    armep --> endpoints
    armep --> arm
    grasp --> endpoints
    grasp --> arm
    grasp --> contracts
    perssess --> endpoints
    perssess --> observe
    perssess --> percon
    store --> percon
    percon --> contracts
    percon --> observe

    verify --> corev["Core: verification/request_builder"]
    agentint --> corea["Core: agent/loop · tools/image"]
```

`contracts.py` 是全项目的契约真源：`TOOLS` / `ARM_TOOLS` / `GRASP_TOOLS` /
`OBSERVE_TOOLS` / `PERCEPTION_TOOLS` 这些字典被 `scripts/build.py` 直接导入，
用于生成 Gateway 的 ToolSpec 与 JSON Schema。

## 4. 工具与 Skill 能力矩阵

```mermaid
flowchart LR
    subgraph BASE["三个 Skill 共有"]
        T1["g1d.state — query"]
        T2["g1d.camera_snapshot — query"]
        T3["g1d.observe — query"]
    end

    subgraph LIFT["g1d-lift 追加"]
        T4["g1d.set_height — action"]
    end

    subgraph ROBOT["g1d-robot 追加"]
        T5["g1d.perception_state — query"]
        T6["g1d.perception_session — action"]
        T7["g1d.arm_state — query"]
        T8["g1d.move_joints — action"]
        T9["g1d.grasp_target — action"]
    end

    subgraph FLAGS["节点启动开关"]
        F1["--allow-height"]
        F2["--allow-arm"]
        F3["--allow-perception"]
    end

    BASE --> LIFT
    BASE --> ROBOT
    F1 -.-> T4
    F2 -.-> T7
    F2 -.-> T8
    F2 -.-> T9
    F3 -.-> T5
    F3 -.-> T6
```

只有声明了对应路由的 profile 才会注册并发布该端点；`g1d-robot` 不启用升降，
所以它的 real dataflow 只带 `--allow-arm --allow-perception`。

## 5. Action 生命周期时序（以 `g1d.set_height` 为例）

```mermaid
sequenceDiagram
    autonumber
    participant A as Core Agent / ForgeToolClient
    participant G as Gateway
    participant N as g1d_node Endpoints
    participant L as LiftAction
    participant D as Device native 或 mock

    A->>G: HTTP 调用 g1d.set_height
    G->>N: lift_tool_in start 信封
    N->>L: start request / context / events

    Note over L: 接纳前全部门禁<br/>enabled · 行程 · timeout_s · Gateway deadline<br/>忙或熔断 · 初始反馈新鲜 · 看门狗 · 序号去重
    L-->>N: ToolAccepted
    N-->>G: lift_tool_out
    G-->>A: 接纳响应（接纳 ≠ 完成）

    L->>L: asyncio.create_task(_run)

    par 后台闭环
        loop 每 50 ms 直至终态
            L->>D: read
            D-->>L: height / age / seq / watchdog
            L->>L: cancel · deadline · link_ok · 新鲜度 · 范围
            L->>D: command clamped kp 速度
        end
    and Agent 轮询
        A->>G: status / result
        G->>N: lift_tool_in
        N->>L: status 或 result
        L-->>A: phase 或 pending
    end

    Note over L: 进入容差后要求新序号反馈稳定 settle_s
    L->>D: 零速命令
    L->>D: read 直至稳定观测
    Note over L: 始终走 _stop_and_observe<br/>停止无法确认 → unknown + faulted 熔断
    L->>L: 先写 goal.result，再发事件
    L-->>G: ToolEvent executor_completed / failed / cancelled
    A->>G: 最终 result
```

`JointAction`、`GraspAction`、`PerceptionSession` 都**只继承 `LiftAction` 的
`status/result/cancel`（以及 `_Goal`）**，物理行为各自实现。

## 6. 升降闭环控制流程

```mermaid
flowchart TD
    S["start 请求"] --> P1{"height.enabled?"}
    P1 -- 否 --> REJ1["G1D_CONTROL_DISABLED"]
    P1 -- 是 --> P2{"目标在 min..max 内?"}
    P2 -- 否 --> REJ2["G1D_HEIGHT_OUT_OF_RANGE"]
    P2 -- 是 --> P3{"timeout_s ≤ max_duration_s?"}
    P3 -- 否 --> REJ3["G1D_DEADLINE_LIMIT"]
    P3 -- 是 --> P4{"Gateway deadline 剩余 > 0?"}
    P4 -- 否 --> REJ4["G1D_DEADLINE_EXPIRED"]
    P4 -- 是 --> P5{"active 或 faulted?"}
    P5 -- 是 --> REJ5["G1D_BUSY_OR_UNCERTAIN"]
    P5 -- 否 --> P6{"初始反馈新鲜且看门狗正常?"}
    P6 -- 否 --> REJ6["G1D_STATE_STALE"]
    P6 -- 是 --> P7{"execution_key 未见过?"}
    P7 -- 否 --> REJ7["G1D_DUPLICATE_GOAL"]
    P7 -- 是 --> ACC["ToolAccepted + create_task"]

    ACC --> LOOP["每 50 ms 循环"]
    LOOP --> C1{"cancel?"}
    C1 -- 是 --> STOP["break: cancel_requested"]
    C1 -- 否 --> C2{"超过 deadline?"}
    C2 -- 是 --> STOP2["break: execution_timeout"]
    C2 -- 否 --> C3{"link_ok?"}
    C3 -- 否 --> STOP3["break: gateway_lease_path_lost"]
    C3 -- 是 --> C4{"反馈新鲜?"}
    C4 -- 否 --> STOP4["break: feedback_stale"]
    C4 -- 是 --> C5{"看门狗跳闸或超范围?"}
    C5 -- 是 --> STOP5["break: watchdog / 超范围"]
    C5 -- 否 --> C6{"abs 误差 ≤ tolerance?"}
    C6 -- 否 --> CMD["command clamped 速度"]
    C6 -- 是 --> C7{"新序号反馈稳定 settle_s?"}
    C7 -- 否 --> CMD
    C7 -- 是 --> OK["break: target_reached"]

    STOP --> FIN
    STOP2 --> FIN
    STOP3 --> FIN
    STOP4 --> FIN
    STOP5 --> FIN
    OK --> FIN

    FIN["finally: _stop_and_observe<br/>零速 + 稳定观测"] --> F1{"停止已确认?"}
    F1 -- 否 --> UNK["outcome=unknown<br/>reason += :stop_unconfirmed<br/>faulted=True"]
    F1 -- 是 --> F2{"成功但最终高度超容差?"}
    F2 -- 是 --> FAIL["outcome=failed"]
    F2 -- 否 --> RES["写 goal.result 并发终态事件"]
    UNK --> RES
    FAIL --> RES
```

## 7. Dex1 夹爪状态机

```mermaid
stateDiagram-v2
    [*] --> idle

    idle --> open_leg: open
    idle --> open_leg: release
    idle --> close_leg: close

    open_leg --> opened: phase=succeeded 且 abs(q-target) ≤ 0.15
    open_leg --> interrupted: 超时 / 取消 / 链路或反馈不可用

    opened --> [*]: open 直接结束
    opened --> close_leg: release 自动闭回（先 cancel 再下闭合计划）

    close_leg --> fully_closed: 停在 closed_rest 附近
    close_leg --> contact_detected: 停位 > closed_rest + contact_margin
    close_leg --> interrupted: 超时 / 取消 / controller failed

    contact_detected --> held: _hold_at_contact 成功<br/>blockage_rad = 停位角
    contact_detected --> interrupted: hold 未确认

    held --> [*]
    fully_closed --> [*]
    interrupted --> [*]
```

闭合判定细节：稳定 0.4 s 且 `reference - q ≤ 0.18` 才算停位；停位高于
`closed_rest + contact_margin_rad` 判 `contact_detected`，否则 `fully_closed`。
`release` 的 `timeout_s` 必须同时覆盖开爪腿与闭回腿，否则接纳阶段就报
`GRASP_DEADLINE`。`open` / `close` / `release` 与 `move_joints` 共享同一把
`joints.lock` 与 `reservation`，全机同时只有一个控制者。

## 8. 持续感知时序

Gateway 1.0.2 不支持 session 语义，所以持续感知以**限时 Action + 独立 Query**
组合实现：Action 只抓帧落盘，Query 读最新状态，判断全部由 Agent 的视觉模型完成。

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent
    participant G as Gateway
    participant P as PerceptionSession
    participant C as Cameras
    participant S as StateStore
    participant F as 共享文件系统

    A->>G: g1d.perception_session sources / duration_s / interval_s
    G->>P: start
    P->>C: frame 每路：新鲜度 + 亮度校验
    Note over P: 准入失败立即 PERCEPTION_IMAGE_UNAVAILABLE
    P-->>G: ToolAccepted

    loop 每 interval_s，直到 cancel 或 duration 用尽
        P->>C: frame 取各路最新帧
        C->>F: snapshot_frame 内容寻址写 {source}-{sha256}.jpg
        P->>S: update frames / active=True
        P-->>G: progress 事件 frames_captured
    end

    A->>G: g1d.perception_state max_age_ms
    G->>S: snapshot 窗口检查
    S->>S: 读取时刻重算每帧 age_ms
    S-->>A: 每路恰好 1 帧 image_path
    A->>A: image 工具查看 image_path
    Note over A: 无历史、无检测、无深度<br/>静帧不是视频，不能推轨迹或速度

    A->>G: cancel 或到期
    P->>S: mark_inactive
```

`StateStore` 只保留每路最新 1 帧，且**每次读取都按当前时刻重算 `age_ms`**，
不会把捕获时的年龄冻结下来。

## 9. 构建与打包流水线

```mermaid
flowchart LR
    PREP["scripts/prepare_dependency.py"] --> DEPS[".deps/forge-gateway/src<br/>forge_tool · forge_gateway"]
    CFG["configs/device.yaml<br/>configs/network.yaml<br/>configs/cameras.yaml"] --> BUILD["scripts/build.py"]
    DEPS --> BUILD

    BUILD --> GUARD{"Linux x86_64?<br/>network_interface 与 network.yaml 一致?"}
    GUARD --> CM["cmake -S native -B build/native"]
    CM --> SO["build/native/libg1d_sdk.so"]
    SO --> PYI["PyInstaller scripts/node_entry.py"]
    PYI --> BIN["build/bin/g1d_node"]
    BIN --> ARC["archive_node<br/>gzip mtime=0 可复现"]
    ARC --> NODEART["dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz"]
    NODEART --> LOCK["真实 sha256 → artifacts.nodes.g1d 锁"]

    BUILD --> GWCFG["gateway_config()<br/>从 Pydantic 模型导出 JSON Schema<br/>+ 按 profile 裁剪 route"]
    GWCFG --> VALID["GatewayConfig.from_dict 校验"]
    LOCK --> PROF
    VALID --> PROF["profiles/mock|real/<br/>gateway.yaml · dataflow.yaml · device.yaml"]

    BUILD --> ASSETS["拷贝 cameras.yaml / 三个 .so<br/>收集 license 与第三方声明"]
    ASSETS --> MAN["生成 skill.yaml<br/>load_manifest 校验"]
    PROF --> MAN
    MAN --> PKG["Core scripts/package_skill.py"]
    PKG --> DIST["dist/skills/g1d-observe|lift|robot-*.tar.gz<br/>dist/build-report.json"]
```

## 10. 分层测试

```mermaid
flowchart TB
    T1["tests/test_devices.py<br/>升降边界：拒绝、超时、取消、停止未确认熔断"]
    T2["tests/test_observe.py<br/>观察包：图像钉住、skew 拒绝、无 manifest"]
    T3["tests/test_arm.py<br/>MoveRequest 校验、速度受限时长、取消"]
    T4["tests/test_elbow_frames.py<br/>肘部坐标系正反解与不可达拒绝"]
    T5["tests/test_grasp.py<br/>接触检测、release 双腿、独占控制"]
    T6["tests/test_grasp_verifier.py<br/>已停用验证器的历史行为"]
    T7["tests/test_bundles.py<br/>Skill/Node 用 Core 原生校验器安装"]
    T8["tests/test_gateway.py<br/>真实 Gateway + Core 客户端，仅替换 mock 设备"]
    T9["tests_core/test_agent_*.py<br/>Core 侧 Agent 入口、图片与验证"]

    T8 --> E2E["端到端：Core HTTP 客户端 → 官方 Gateway<br/>→ 官方 Arrow binding → mock 设备"]
```

`tests/test_gateway.py` 是唯一贯穿全链路的一层：它不替换 Gateway，也不替换
`forge_tool` 绑定，只把设备换成 `MockDevice`/模拟相机。

## 11. 关键安全约定

| 约定 | 落点 |
|---|---|
| 严格 Schema，多余字段直接拒绝 | `contracts.StrictModel` |
| 启用运动必须显式确认机械行程 | `HeightSettings.check_limits` / `Dex1Settings.check_travel` |
| 应用包络是“调试确认值”，不是厂商极限 | `arm.LIMITS`、`native/joint_motion.hpp::allowed` |
| 真机启动即收缩 Dex1 包络 | `arm.configure_limits`（mock 保持默认） |
| 任何 Action 都遵守 Gateway 绝对 deadline | `endpoints.LiftAction.start`、`arm_endpoint`、`grasp_action` |
| 收取消后不再发新的非零命令 | `endpoints._run` 循环内二次检查 |
| 所有终态都请求零速并观测停止 | `endpoints._stop_and_observe` |
| 停止无法确认 → `unknown` + 熔断，拒绝再接纳 | `LiftAction.faulted` |
| Gateway 注册确认 > 2 s 未到 → 本地取消 | `node.tick` 的 `link_ok` |
| 命令中断 250 ms / 反馈中断 300 ms → C++ 看门狗零速 | `native/g1d_sdk.cpp` 看门狗线程 |
| 关节写入永不持有反馈互斥锁 | `native/sdk_joint_driver.hpp` |
| 关节控制互斥由文件锁 + `/proc` 扫描保证 | `flock /tmp/paos-g1d-arm.lock` |
| 快照内容寻址且不可变 | `cameras.snapshot_frame` |
| DDS 断流不重复发布旧状态伪造新鲜度 | `node.tick` 序号变化判定 |
