# 参数化关节运动接入

新增 `g1d-robot` Forge Skill，复用 Core、官方 Gateway、Dora 和现有 G1-D 设备节点。
新工具是 `g1d.arm_state`（Query）和 `g1d.move_joints`（Action）。不是将 arm_sequence
二进制包装成一个工具，也不依赖 Agent 以 200 Hz 发关节命令。

## 文件职责

机械臂与 Dex1 的 Python 操作模块统一放在 `src/paos_g1d/manipulation/`。
Python 导入路径使用 `paos_g1d.manipulation.*`；工具 ID、输入输出 Schema、运动参数和现有脚本调用命令保持不变。
共享设备连接与查询仍由包根目录的 `device.py`、`contracts.py`、`endpoints.py` 提供，原生 DDS 控制代码仍位于 `native/`。

| 文件 | 职责 |
|---|---|
| `native/joint_motion.hpp` | 可复用参数化五次轨迹，关节目标、时间、限速与到位判据 |
| `native/sdk_joint_driver.hpp` | 常驻 200 Hz 控制线程、内部 Dex1、动作后保持、取消冻结参考 |
| `native/g1d_sdk.cpp` | 共享原有 DDS 实例，增加 arm move/read/cancel C ABI |
| `src/paos_g1d/manipulation/arm.py` | 严格参数/结果模型、关节名称与应用范围、ctypes 与 mock 后端 |
| `src/paos_g1d/manipulation/arm_endpoint.py` | 官方 Query/Action admission、状态、结果、取消与超时 |
| `src/paos_g1d/manipulation/elbow_frames.py` | 肘部弯曲角、地面仰角的求解与反馈转换 |
| `src/paos_g1d/node.py` | 注册端点、公布 readiness；新增 --allow-arm |
| `scripts/build.py` | 生成 g1d-robot mock/real Gateway ToolSpec、Dora profile 和发布包 |
| `skills/g1d-robot/SKILL.md` | Agent 的角度含义、调用顺序、结果核对和恢复规则 |
| `src/paos_g1d/manipulation/arm_recipe.py` | 参考回放参数；不是注册工具，Agent 入口不调用 recipe() |
| `scripts/replay_arm_sequence.py` | 用 Core ForgeToolClient 逐步重放并保存 invocation/result |
| `scripts/run_arm_agent.py` | 将原始中文 prompt 交给原生 AgentLoop，记录模型工具选择 |
| `scripts/smoke_test.py` | 从打包制品启动独立 mock Gateway/Dora 并运行回放 |
| `tests/test_arm.py`、`native/test_joint_motion.cpp` | 参数边界、任意目标、时序、到位、速度、取消等回归 |

## 单次工具参数示例

```json
{
  "targets": [
    {"joint": "left_elbow", "position_rad": 0.0},
    {"joint": "right_elbow", "position_rad": 0.0}
  ],
  "duration_s": 3.0,
  "timeout_s": 20.0
}
```

工具 ID 为 `g1d.move_joints`。最多指定 14 个手臂关节及两个内部 Dex1；腰部不接受运动目标。
未指定关节保留上一控制目标。支持绝对编码器角度，不支持末端空间位姿、IK 或自动避障。
角度、时间必须有限，重复关节和超出应用范围的目标会被拒绝，不能静默裁剪。
范围属于本应用的受限运动范围，不是厂商完整机械限位。

肩部、肘及 Dex1 到位容差 0.15 rad，腕关节 0.08 rad。五次轨迹结束后持续 0.3 秒反馈到位才成功。
手腕 roll 及肘部参考上限 1.1 rad/s，其余臂关节 0.65 rad/s，Dex1 7 rad/s。
50°/1.5 秒左腕轨迹峰值约 1.091 rad/s；比先前 30° 示例需要更高的 roll 参考限速。
大角度可能自动延长有效轨迹时间；超时必须覆盖有效时长，否则执行前拒绝。
PD 与原组合动作相同；内部 Dex1 电机左31/右33，目标与实测差限幅仍为 0.18。

## 复现组合动作

交给 PhyAgentOS 的文字原样为：

> 抬起双肘90度，左臂旋转50度，夹爪打开，然后逆向此过程

按本次用户前文约定，左臂旋转指左腕 roll，并非肩部旋转；夹爪指左侧 dex1_internal。
标准垂臂：肩腕零位、双肘 pi/2；视觉弯肘90°：双肘编码器零位。

| 顺序 | g1d.move_joints 目标 | 请求轨迹时间 |
|---|---|---|
| 1 | 初始化双臂垂下，保留两夹爪开合 | 5s；Agent 查状态已到位可跳过 |
| 2 | left_elbow=0、right_elbow=0 | 3s |
| 3 | left_wrist_roll=0.8726646259971648 | 1.5s |
| 4 | left_dex1=5.4（0.1.11 前；现为 device.yaml 里经实测确认的开爪目标） | 1.5s |
| 5 | left_dex1=执行前记录的开合 | 1.5s |
| 6 | left_wrist_roll=0 | 1.5s |
| 7 | 双肘=pi/2 | 3s |

参数化单次 Action 始终遵循显式轨迹与到位确认；它不内置旧程序的初始化阶段特例。
Prompt 已要求逆向，因此打开完成后立即开始恢复，不等待 A 键。
完成工具任务后控制器继续保持；不是每完成一步就退出程序。

## 构建与 mock 验证（在 PhyAgentOS-g1d 目录）

```bash
.venv/bin/python scripts/build.py
.venv/bin/python -m pytest -q
ctest --test-dir build/native --output-on-failure
.venv/bin/python scripts/smoke_test.py --arm --port 19012 \
  --gateway-archive dist/vendor/gateway-1.0.2.tar.gz --dora .tools/dora
```

最后命令安装到临时测试目录、仅选择 mock profile，端口19012，避免影响现有19002服务。
保存 `dist/arm-replay-report.json`，含原始 prompt、各调用的参数/ID/结果和制品摘要。
这是确定性工具回放（llm_used=false），不能算模型自主规划或真机验收。

## 让 PhyAgentOS 自主选择工具

制品为 `dist/skills/g1d-robot-0.1.1.tar.gz` 和更新后的 `dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz`。
在运行 PhyAgentOS 的相同实例中，用原生 skill install / forge-node install 安装 bundle 与其
锁定 node archive；Gateway archive 复用官方版本。具体 node lock 标识读取新 skill.yaml，
不要复用旧程序的节点摘要。使用 `scripts/core-env.sh` 进入已有 Core 依赖与实例环境。

```bash
./scripts/core-env.sh paos skill install dist/skills/g1d-robot-0.1.1.tar.gz --local
./scripts/core-env.sh paos forge-node install g1d-robot gateway --archive dist/vendor/gateway-1.0.2.tar.gz
./scripts/core-env.sh paos forge-node install g1d-robot g1d --archive dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz
./scripts/core-env.sh paos skill start g1d-robot --profile mock
./scripts/core-env.sh python scripts/run_arm_agent.py --profile mock
```

如果当前实例已有其他活动 Skill/AgentTask，先按 Core 生命周期核对和切换，不要直接停止真机
控制服务来腾端口。上述 Agent 入口只发送该原始 prompt，不注入参考调用序列。
它要求已启动并匹配 profile 的 g1d-robot，输出 `dist/arm-agent-*.json`。模型结果需单独核对；
本次完成的是 mock 工具回放，未运行模型闭环，也未安装或替换当前正在运行的实例。

真实 profile 需要在现场测试条件就绪后显式启动；启动本身只订阅，首次 Action 才创建发布器。
旧 arm_sequence 等 SDK 动作程序必须先正常回位退出；NativeArm 对已知同机测试程序做拒绝检查，
新的 native 控制器之间使用进程锁。该检查不能发现任意远端 DDS 发布者，部署必须保证唯一控制者。

取消只冻结当前参考并持续控制，不等价于逆向回位或物理停止确认。异常/unknown 不自动重试。
正常逆向由 Agent 按夹爪、腕、肘的顺序提交工具。停止整个 Runtime 会终止 DDS 保持，
应先完成回位及现场接管；不可把 task finalize 当作 runtime stop。

0.1.1 删除原 ArmRuntime 实现，SDK 初始化与 Write 均在反馈锁外，短暂陈旧反馈仅暂停轨迹，DDS 写入失败记录明确诊断；结果仍需真实反馈到位。原始 Forge Tool API 保留用于 PhyAgentOS 调用。

## 0.1.1 真机验收

已通过官方 Gateway 逐步执行真实初始化、抬肘、左腕50°、左内部Dex1打开，以及逆向恢复共七个 Action，全部 succeeded；报告 `dist/arm-real-replay-report.json` 保存各步真实角度、invocation 和制品摘要。抬肘实测约0.109/0.097 rad，左腕约0.867 rad，夹爪打开约5.348 rad（当时的 vendor 映射常量目标；
2026-09-11 实测该手当前只能到约 3.36 rad，5.4 并不可达，0.1.11 起改用 `configs/device.yaml`
中经操作员确认的开爪目标）；最终双肘约1.548/1.560 rad，腕回零附近。验收使用确定性工具回放，
不等同于模型自主规划验收。Runtime 保持运行。

## 自定义自然语言 prompt

`run_arm_agent.py --prompt '任意新指令'` 会原样覆盖默认组合动作。省略参数仍使用原始 prompt，空白参数被拒绝。示例：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy \
./scripts/core-env.sh python scripts/run_arm_agent.py --profile real \
  --prompt '抬起左臂30度，然后抬起左手肘到90度，旋转50度，开合夹爪，然后逆向执行此过程'
```

0.1.2 Skill 增加通用坐标与逆序规则，不要求新指令照搬旧双肘例子。
本次已将该 prompt 交给真实 Agent，模型发出正确左肩目标 -0.523599 rad。实测停在 -0.418621 rad，超出肩部 0.08 rad 到位容差，首步失败；后续阶段未执行。已单独通过 Forge 工具将左肩恢复，结果 succeeded。记录见 dist/arm-agent-20260910T061738.793652Z.json。本次不能视为完整组合动作成功。

### 0.1.4：迁移 G1 官方手臂 PD 增益

按用户确认 G1/G1-D 上肢一致，迁移 Unitree `xr_teleoperate/teleop/robot_control/robot_arm.py` 中 G1_29_ArmController 的手臂增益：肩/肘 Kp=80、Kd=3，腕 Kp=40、Kd=1.5。腰部与 Dex1 不变，tau 前馈仍为 0。肩/肘/Dex1 容差仍为 0.15 rad，腕为 0.08 rad，便于与此前同轨迹对比。

`run_arm_agent.py` 对每个已返回的动作结果打印全部关节参考角、实际角和绝对误差（rad/deg），包括容差内的误差。结果到位后的采样不等于长期稳态误差；判定精度时对比相同姿态、负载、保持时间。此次未增加重力补偿。

### 0.1.5：肘部角度参考系

新增 `--elbow-mode upper_arm|world` 和可选 `--elbow-angle-deg`，默认 upper_arm。
`--prompt` 保持原文记录，运行时额外传入参考系约定；角度覆盖仅用于正向抬肘，逆向恢复动作前编码器值。

- upper_arm：现有标定约定，伸直 0°、直角弯肘 90°；编码器目标为 π/2 − bend。
- world：前臂纵轴（腕 roll 轴 +X）相对水平面的仰角，向下 −90°、水平 0°、向上 +90°。通过 `rt/secondary_imu` 躯干 RPY 和肩 pitch/roll/yaw 实测角进行三维转换，含肩部安装倾角。左右手均支持。
- world 是单次前臂仰角目标，不控制末端 XYZ 位置或方位角，也不在后续肩部/底座运动中持续锁定。肩部先单独执行，再执行肘部；肩部和基座在这一动作中保持不动。模型轴向角不同于用肘心到腕心连线测量的角度。
- IMU/关节反馈须新鲜；缺少 IMU、不可达或越过既有肘部范围 [-0.2, 2.0] rad 时拒绝动作，不截断、不擅自改变肩部。±90°定义有效，但某些肩部姿态只能动肘无法到达。
- 输出增加 `elbow_bend_deg`、`forearm_world_elevation_deg`、`torso_rpy_rad`、`torso_age_s`，动作结果包含 `requested_elbows` 和 `resolved_targets`。到位还会验证所选参考系角度，沿用 0.15 rad 容差；世界角是模型+编码器+IMU估计，不代表外部标定精度。

几何来源：https://github.com/unitreerobotics/xr_teleoperate/blob/main/assets/g1/g1_body29_hand14.urdf
（2026-09-10 核对肩部 joint origin/axis，torso IMU 固定姿态为零；沿用用户确认的 G1/G1-D 相同上肢）。

```bash
# 上臂先抬30°，再相对上臂弯肘90°
./scripts/core-env.sh python scripts/run_arm_agent.py --profile real \
  --elbow-mode upper_arm --elbow-angle-deg 90 \
  --prompt '抬起左臂30度，再抬起左肘，然后逆向恢复'

# 上臂先抬30°，再使前臂相对地面水平（0°）
./scripts/core-env.sh python scripts/run_arm_agent.py --profile real \
  --elbow-mode world --elbow-angle-deg 0 \
  --prompt '抬起左臂30度，再抬起左肘，然后逆向恢复'
```

底层仍用 `g1d.move_joints`，例如：
```json
{"elbows":[{"side":"left","mode":"world","angle_deg":0}],"duration_s":3,"timeout_s":20}
```
这两个模式必须安装 0.1.5 Skill 和对应 Node；只更新启动脚本不够。此版本的 world 模式尚需用户实机验证 IMU/模型方向与实际前臂一致；开发测试不会驱动机器人。
