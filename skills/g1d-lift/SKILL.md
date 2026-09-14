---
name: g1d-lift
description: Observe G1-D and execute one bounded column-height action through Forge.
metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["g1d-lift"]}}}
---

# G1-D bounded column motion

Use for an explicitly requested column-height change. Activate as primary, obtain live
`forge_tool_context`, and create an AgentTask before calling the Action. Use only
`g1d.state`, `g1d.camera_snapshot` and `g1d.set_height` via native Forge tools.

1. Check Runtime/profile identity and `simulated`. Mock execution is not hardware validation.
2. Require a ready lift Endpoint with `enabled` and confirmed limits. Read the current state
   and available camera snapshots. A disabled Endpoint or unknown height coordinate is a deployment
   issue; do not enable it by editing files or running a shell command from the Agent.
3. Resolve the target in metres in the column's own feedback coordinate. Do not substitute robot
   total height, ground height, arm TCP height or a guessed zero point. Use an explicit target,
   tolerance and duration permitted by live ToolSpec and device limits.
4. Bind observations and `forge_tool_start_action` for `g1d.set_height` to the same AgentTask.
   Only one column action is permitted at a time. There are no arm/base/gripper Actions in this Skill.
5. Retain Gateway invocation and attempt IDs independently of task ID. Poll native action status
   and result with bounded intervals. Admission and pending results do not establish completion.
6. For cancellation, call `forge_tool_cancel_action` once and continue reconciliation. A cancel
   accepted response does not prove physical stop. Require the terminal execution facts.
7. Inspect `reached_goal`, `final_height_m`, `tolerance_m`, `stop_command_accepted` and
   `stopped_observed`. The last field means fresh column feedback stayed within the configured
   stop tolerance; it is not an independent drive/brake acknowledgment.
8. On timeout, stale feedback, command failure or `unknown`, reconcile the existing invocation
   and current state. Do not blindly repeat the Action. An unresolved stop blocks new local motion.
9. After all owned actions are terminal, finalize the AgentTask using Core's existing criteria,
   evidence and verifier. Executor success alone does not prove the user's whole goal. Add a new
   PlanRevision only when the native recovery verdict allows it.

For visual analysis, pass the snapshot Query's same-host `image_path` to the existing `image`
tool with `mode: vision`; use the configured Provider. The head image is one stereo pair: two
640x480 eyes side by side, at equal sharpness.
Never bypass Forge with vendor binaries, direct DDS/SDK commands or a custom Agent motion tool.

## Relative-height requests

For a user request such as rising or descending a stated distance, first read fresh g1d.state
and the live lift context. Convert centimetres to metres and compute target = current height
+ signed displacement (positive upward, negative downward). Never reuse an earlier conversation's
height. If the target is outside the confirmed limits, do not move or silently clamp it.

Read the column height alone with `{"max_age_ms": 100, "joints": []}`: the lift workflow needs
nothing from the 16 joint axes, and carrying them through the conversation only makes every later
turn slower. That unbound query is allowed only while resolving the target, before the task exists.

Create the AgentTask before motion, and create it **before the first execution call**:
every execution and lifecycle tool, and every Query whose result becomes evidence, must carry that
same `task_id`. A call without it is rejected, and the rejected round still costs a full model turn.
Its goal and success criteria must state the user's signed displacement, measured starting height,
calculated absolute target, and tolerance.
Require exactly one successful g1d.set_height Action, final height and signed displacement within
0.002 m of the goal, reached_goal=true, matching simulated identity, stop_command_accepted=true
and stopped_observed=true.
Use evidence_policy required_kinds=["robot_state"], minimum_association="best_effort".

### Verification mode

Use `mode="off"` for this workflow. Every criterion above is a field the Action computes and
enforces itself: it fails the execution when the final height is outside `tolerance_m`, and it
refuses a second goal while one is active. A semantic verifier therefore adds no information here,
and on this deployment it has marked successful Actions as failed when its response was truncated.
If a verdict is explicitly required, use `audit` rather than `enforce`: audit still records the
verifier's verdict but decides the task from the execution facts, so a verifier that errors cannot
fail a successful Action. Do not add RGB evidence or a second Action to compensate.

Use tolerance_m=0.002 and a timeout_s no greater than the live configured maximum.
The Gateway timeout_ms should allow the Action duration plus 10 seconds for completion handling.
Submit at most one movement Action for one relative-height request. Read its terminal result,
query the final height, and finalize the AgentTask. Additional corrections, or a second Action to
turn a failed acceptance into success, are not part of this workflow.
Treat a request as an instruction to perform the specified movement within the commissioned
deployment; ask only when the target, live limits, readiness or required operating conditions
are missing. Do not invent safety clearance from camera images.
