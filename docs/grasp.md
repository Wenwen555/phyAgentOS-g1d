# 人工对准物品的 Dex1 操作

`g1d-robot` 0.1.12 已关闭 verifier。左右手共用同一实现，侧别由当前 prompt 决定。
用户把物品对准张开的夹爪，机器人负责打开、闭合和保持；不包含自动移动手臂对准物品。

闭合不再要求 `before_observation_id`、视觉预检通过、60 秒有效期或抓前姿态匹配，也不再强制抓后采图。
Agent 不再注册 `grasp_verify`，不会自动发起抓前/抓后模型调用。`g1d.observe` 与 `image` 仍可按需独立调用。

0.1.11 起，开爪目标不再是 vendor 遥操作映射常量 5.4，而是 `configs/device.yaml` 中经实测确认的
Dex1 行程；闭合改为“边闭边看编码器”：手指被物品挡住时会在空夹静止位之上停住，该停位即接触，
闭合就此结束并在停位保持，不再继续向 0 压。

0.1.12 起加入**角度管理**，`operation` 有三个取值：

| operation | 行为 | 终止状态 |
|---|---|---|
| `open` | 开到配置的开爪目标并保持 | `opened` |
| `close` | 闭合，遇阻在停位保持，否则到空夹静止位 | `contact_detected` / `fully_closed` |
| `release` | 先开到配置的开爪目标，**确认到位后自动再闭合一次** | `released_then_closed`（空）/ `contact_detected`（仍被挡住） |

两条角度规则：**夹住即锁定角度**——判定接触后手指停在并保持在测得停位 `blockage_rad`，
这个角度就是本次抓取的确认角度，下一次命令之前不会漂移；**松开即回到闭合位**——
`release` 不允许把夹爪停在张开状态，放完物品后由 Action 自己闭回空夹静止位。
`release` 的 `timeout_s` 必须覆盖“开 + 闭”两段行程，只够一段时会被 `GRASP_DEADLINE` 拒绝。

## PAOS 使用方式

更新后重新进入 Agent，以刷新工具列表和 Skill 指令：

```bash
./scripts/core-env.sh paos agent
```

先输入：

> 打开右手夹爪，等我把物品放好，先不要夹住。

放好后输入：

> 闭合右手夹爪并保持。

左手把“右手”改为“左手”。已有明确侧别的连续对话可以沿用该侧别。
打开和闭合分别提交 Action，打开后不会定时自动闭合。

直接工具调用：

```json
{
  "tool_id": "g1d.grasp_target",
  "arguments": {
    "side": "right",
    "operation": "close",
    "duration_s": 1.5,
    "timeout_s": 10.0
  }
}
```

Agent 将其绑定到当前 AgentTask，再轮询 Action 到终态。
任务成功标准为所请求的机械动作完成，不要求图像或“已视觉确认抓住”。

## 返回结果

- `opened`：已打开所选夹爪到确认的开爪目标。
- `contact_detected`：手指在空夹静止位之上停住（编码器稳定 0.4 秒、变化不超过 0.03 rad），
  判定为接触并将手指保持在停位；`blockage_rad` 是测得停位角度，即本次确认的夹持角度。
  属机械证据，不等于“抓住了目标物品”。
- `fully_closed`：手指到达空夹静止位，编码器没有看到阻挡；对薄或软物品不能据此排除实际上已夹住。
- `interrupted`：动作未正常完成。

`reason` 在成功时区分具体路径：`waiting_for_user_placement`（open）、
`contact_detected_reference_held` / `contact_hold_unconfirmed`（close 或 release 的闭合段）、
`bounded_close_complete`（空夹闭合）、`released_then_closed`（release 放完并已闭回）。

`verification_status` 固定为 `disabled`，`grasp_verified` 固定为 `false`。
为兼容结果读取，`before_observation` / `after_observation` 字段保留但均为 null。
成功返回表示机械动作完成，不表示视觉验证或夹持力验证通过。

## 视觉放置触发闭合（0.1.11；0.1.16 起物体认腕部、手指认头部；0.1.17 起一次读取即闭合）

仅当用户明确要求“视觉检测到我放好之后自行闭合”时启用；其余情况仍走上面的人触发流程。

它是 **Agent 驱动的轮询闭环，不是自动触发器**：这个 Gateway 没有 session 语义、没有条件/触发
原语（`handler.py:855` 抛 `NotImplementedError`，控制通道只认 cancel，`handler.py:828`），
`grasp_target` 不做图像检查，感知也不提供检测/深度/三维位置。所以闭环由 Agent 自己串起来：

```
抬肩 → 抬肘 → 开爪 → 起 perception_session（left_wrist + head）→ 每轮读 perception_state + 两路各判一半
     → 物体（腕部帧）与手指/人手（头部帧）同一次读取里都成立 → 自行 close（编码器接触停位）
     → 按同一分工复核夹住的物品 → 确认或 release 放回闭合位
```

要点：

| 项 | 值 | 原因 |
|---|---|---|
| 判物体 → 腕部 `left_wrist` | 是不是要抓的那个物体、有没有到夹爪口 | 抬臂姿态下物体在头部画面里可能太小或太淡，头部不一定分辨得出；腕部近距离拍得到物体，16:56 实拍帧里白盒占了大半画面 |
| 判手指与人手 → 头部 `head` | 手指是否张开可见、指间是否空着、夹爪附近有没有人手 | 同一姿态下腕部帧通常拍不到手指（0.1.10 那轮就是因此换的观察源），手指只有头部看得见 |
| 判定分工不可互换 | 两半都要在同一轮成立，头部帧单独不能触发闭合、腕部帧单独也不能 | 单视角推断正是之前的失败点 |
| 退出路径 | 某一路不再能判它那一半（头部看不到手指 / 腕部看不到夹爪口）就取消会话、说明是哪一路失效，退回人触发 | 判据缺一半就不许闭，也不允许拿另一路的结论顶上 |
| 抓取后复核 | 同一分工：腕部判夹住的是什么、头部判手指是否环绕着它 | 与闭合判据保持一致 |
| `duration_s` | ≤ 300（默认仍 30） | 人放置常超过 1 分钟 |
| `timeout_ms` | 330000 | profile 的 `invoke_timeout_ms`；小于 `duration_s` 会被截断，等于则与网关期限同 tick 竞态，可能被判 `unknown` 并丢结果 |
| 轮询 `max_age_ms` | 1000 | 存储每 `interval_s` 写一次，卡太紧会把正常抖动变成 stale |
| 每轮读取 | `perception_state` 每源只返回**最新 1 帧**（0.1.13 起无 `history_s`/`max_history`） | 一轮一帧、判据单一；要看过程就按 `interval_s` 反复读并比较自己看过的帧 |
| 闭合条件 | **一次读取即确认**（0.1.17）：该次读取的两路帧同时成立就闭合，不再要求连续两轮 | 操作员要求一次即可触发；代价是单次误判也会闭合，所以仍要求两路都成立、紧接读取后立即下发 |
| 闭合判定 | 编码器停位 > 空夹静止位 + 0.03 rad | 手指被挡住时天然停在非零位置 |
| 释放方式 | `operation:"release"` | 放完物品后夹爪自动闭回空夹静止位，不会留在张开状态 |

安全约束（写在 SKILL.md 里，Agent 必须遵守）：

- 起会话后**先确认张开的手指在画面里可见**；哪一路看不到它该判的那一半就取消会话、如实报告是哪一路失效、退回人触发，**不允许盲闭**。
- 判定必须同时满足“物品在指间”与“夹爪附近无人手”。
- **物体认腕部、手指认头部**（0.1.16 起）：物品是否是目标、有没有到夹爪口，只看腕部帧；
  手指是否张开可见、指间状态与人手有无，只看头部帧。**不允许**用头部帧单独判物体（分辨不出），
  也不允许用腕部帧单独判手指（拍不到），更不能拿一路的结论替另一路。
- **一次确认即闭合**（0.1.17）：两半在**同一次读取**里同时成立就下发闭合，不再等第二轮。
  两路仍然缺一不可，读取必须紧接 Action 且帧的 `age_ms` 在界内；判据不成立就继续看，
  不做第二次"确认"，也不拿没刚读过的旧帧当依据。
- 某一路不再能判它那一半（头部看不到手指 / 腕部看不到夹爪口）时，该轮不成立；
  持续如此就取消会话、说明哪一路失效并退回人触发，而不是降低判据。
- 闭合返回后必须复核夹住的物品，分工不变：`g1d.camera_snapshot {source:"left_wrist"}`
  判夹住的是什么，`{source:"head"}` 判手指是否环绕着它；某一路答不了它那一半就如实说，
  不要拿另一路顶替。是目标物品才报告成功；看到手、错物品或什么都没夹住，立即 `release`
  放开并如实报告。
- 用户喊停或抽手：先 cancel 闭合调用，再处理会话。
- 闭合前必须先 cancel 会话——`forge_task_finalize` 在任务名下仍有非终态 Action 时会拒绝。

成本与限制：

- 每一轮 = 一次完整模型轮次 + 一次视觉输入，实际秒级；受 `maxToolIterations`（`.paos-instance/config.json`，默认 40）预算约束，本轮已改为 120。
- 2D 定性判断，无深度与标定；两路各判一半（腕部判物体、头部判手指与人手），
  任一半都只是目视判断，没有测量位置、距离或夹持力，也不能用一路的结论补另一路。
  0.1.17 起一次读取即闭合，单次误判会直接触发闭合——这是操作员明确选择的取舍。
- `verification_status="disabled"` 与 `grasp_verified=false` 描述的是 Action，不是这次视觉判断。
- 会话帧按内容寻址落盘在 `$PAOS_G1D_SNAPSHOT_DIR`，300 s × 0.5 s 最多约 600 张 JPEG，没有自动清理。

## 代码与保留的运动控制

| 文件 | 职责 |
|---|---|
| `src/paos_g1d/manipulation/arm.py` | 已有关节和 Dex1 后端 |
| `src/paos_g1d/manipulation/grasp_action.py` | 直接开合、机械稳定判据、保持；不依赖相机或模型 |
| `src/paos_g1d/manipulation/grasp_verifier.py` | 已停用，保留原实现供离线参考 |
| `src/paos_g1d/agent_integration.py` | 保留独立 image 工具，移除 grasp_verify 注册 |

Dex1 仍使用电机 31/33、原有轨迹和增益，目标相对实测的限幅仍为 ±0.18 rad。
保留速度限制、状态新鲜度、执行期限、控制互斥、取消和动作后保持。
闭合过程中实测角度须稳定 0.4 秒（变化不超过 0.03 rad）才判定停位或完全闭合；判定接触时先
cancel 当前闭合计划，再以停位角度重新下发 0.5 s 的保持命令（运行中的计划会拒绝新计划，
必须先 cancel），控制器阶段到 succeeded 后由收尾 cancel 把参考冻结在该角度。停位判定还要求
`reference - q ≤ 0.18`，避免保持命令在闭合轨迹仍在下降时向外张开。
取消冻结参考。`release` 的实现是在开爪到位后由 Action 自己 cancel 再下发一段闭合轨迹，
因此它的闭合段走同一套接触判定：物体还卡在指间时 release 会再次报 `contact_detected`，
说明这次命令并没有把物品放开，而不是静默地当作已释放。

## 构建与安装

```bash
.venv/bin/python scripts/build.py
.venv/bin/pytest -q
./scripts/core-env.sh python -m unittest discover -s tests_core -q
```

机械臂归位后更新运行时：

```bash
./scripts/core-env.sh paos skill stop g1d-robot
./scripts/core-env.sh paos skill install dist/skills/g1d-robot-0.1.17.tar.gz --local --yes
./scripts/core-env.sh paos forge-node install g1d-robot gateway --archive dist/vendor/gateway-1.0.2.tar.gz
./scripts/core-env.sh paos forge-node install g1d-robot g1d --archive dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz
./scripts/core-env.sh paos skill start g1d-robot --profile real
./scripts/core-env.sh paos skill status g1d-robot
```
