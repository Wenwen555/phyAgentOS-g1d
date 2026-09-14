---
name: g1d-robot
description: G1-D robot observation, state queries, parameterized arm motion and internal Dex1 control.
metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["g1d-robot"]}}}
---

# G1-D robot observation and motion

Activate as primary. Use native Forge tools and an AgentTask; never invoke SDK, shell binaries,
or direct HTTP from the Agent to bypass the runtime. Inspect runtime/profile identity.
`g1d.move_joints` and `g1d.grasp_target` produce arm/gripper effects, `g1d.arm_state` reports them,
and both Actions share one control owner. Do not require column-height feedback to interpret arm
state. Record simulated identity.

## Task binding and probe discipline

Create the AgentTask before the first execution call. Every execution and lifecycle tool
(`forge_tool_start_action`, `forge_tool_action_status`, `forge_tool_action_result`,
`forge_tool_cancel_action`, and every `forge_tool_query` whose result becomes evidence) must carry
that same `task_id`. A call without it is rejected, and the rejected round still costs a full model
turn. An unbound `g1d.state`/`g1d.arm_state` query is allowed only while resolving the user's target
before the task exists.

Call `forge_tool_context` once for each `g1d.*` tool before you invoke it, and only for those tools.
**The returned ToolSpec is the authoritative source** for that tool's parameters, application
envelopes, tolerances and output semantics; this Skill describes workflow and policy, not the
mechanics, so never work from memory of them. Do not re-probe a tool already read in this task, and do
not probe a tool you will not call.

## Joint motion

`targets` is a list of `{joint, position_rad}` in absolute encoder radians. Alternatively `elbows`
accepts `{side: left|right, mode: upper_arm|world, angle_deg}`; the two forms cannot target the same
elbow in one request.

Batch every joint that can move together into one request; `g1d.move_joints`'s ToolSpec carries the
trajectory, settling and shoulder-ordering rules. Batching this way is not the "single sequence tool"
this Skill forbids: it is still one trajectory under the same validation and feedback checks.

`duration_s` is the requested trajectory time; `timeout_s` must additionally cover the speed-limited
duration, the settling window and possible tracking delay — use timeout_s=20.0 for the example below.
Check live schemas and envelopes; large rotations can take longer than the requested time. The native
controller keeps publishing after Action completion, so unspecified joints hold their last references.
One motion at a time across arms and grippers. No Cartesian IK or collision planning is provided; do
not infer obstacle clearance from this API.

## General instructions and G1-D joint coordinates

The example below is NOT a mandatory sequence. Follow the CURRENT user instruction; when only the
left arm is requested, do not move the right arm or initialize both arms. Query fresh state first and
retain pre-action reference/feedback targets for reverse execution. For a forward raise from a
neutral shoulder, shoulder_pitch is NEGATIVE: raising the left upper arm 30 degrees from neutral
targets left_shoulder_pitch=-0.5235987755982988 rad. A wrist rotation is relative to its starting roll
reference unless the user asks for an absolute angle.

In the original example below, an unqualified rotation means left_wrist_roll and gripper means left
internal Dex1. For new grasp requests, select left/right from the CURRENT prompt or its explicit
ongoing placement context; ask which side if missing, and never silently default to left. In a
reverse motion sequence, opening/closing means open to the gripper's own recorded opening, or to the
operator-confirmed open target, then restore the recorded opening. For reverse execution, undo
changed joints in reverse order, preserving unrelated joints. Use shoulder/elbow duration_s=3.0,
wrist/Dex1 duration_s=1.5 and timeout_s=20.0, allowing speed-limited duration extension. Do not
demand the old fixed example's targets for a new prompt.

## Interpretation of the original example sequence

User prompt (preserve verbatim):
> 抬起双肘90度，左臂旋转50度，夹爪打开，然后逆向此过程

For this commissioned workflow, “双肘90度” is the visual right-angle bend: with shoulders neutral,
elbow encoders left_elbow/right_elbow=0.0, NOT pi/2 (hanging elbows are pi/2). “左臂旋转50度”
follows the user's existing arm_sequence convention: left_wrist_roll rotates +50 degrees about the
forearm axis, not shoulder yaw. “夹爪” means left internal Dex1 motor31, not RH56 or external Dex1.
Explain this interpretation; ask if a subsequent user changes it.

1. Query g1d.arm_state and retain initial left_dex1/right_dex1 positions for restoration.
2. Initialize both arms: shoulder pitch/roll/yaw and wrist roll/pitch/yaw = 0.0, both elbows =
   1.5707963267948966, no gripper command. duration_s=5.0. If all arm positions already satisfy the
   published tolerances, skip initialization. This is controlled lowering.
3. Raise both elbows to 0.0, duration_s=3.0.
4. Move left_wrist_roll to 0.8726646259971648, duration_s=1.5 (50 degrees from neutral).
5. Move left_dex1 to the operator-confirmed open target (the vendor mapping constant 5.4 may exceed
   the real finger travel; the tool result and the g1d.grasp_target opened outcome report the
   configured target), duration_s=1.5.
6. After opening completes, immediately reverse because this prompt explicitly requests it: restore
   left_dex1 to its recorded initial position (1.5s), restore left_wrist_roll to 0 (1.5s), then both
   elbows to pi/2 (3s). Do not restore the arbitrary pre-initialization arm posture. If the initial
   gripper was open, restoration means open, not forced closing.
7. Read final arm state and finalize the AgentTask with evidence and explicit criteria.

Submit each move as `forge_tool_start_action` for g1d.move_joints, bound to the same task_id. Retain
invocation/attempt IDs, poll status/result, and wait for a succeeded result plus fresh feedback
within tolerance before the next step: admission is not completion. Do not issue all steps
concurrently or replace this workflow with a single fabricated “sequence” tool. Use before/after
camera snapshots and robot state when the configured verification contract requires them. Do not
claim physical verification for mock. The local replay harness tests tools without an LLM.

## Cancel and recovery

Cancel requests freeze the current reference and keep the controller alive; they do not perform
reverse recovery or prove physical stopping. Reconcile the current invocation and fresh feedback
before planning restoration, in gripper → wrist → elbows order, only for stages actually touched. For
unknown outcomes, stale feedback, or lost runtime, do not blindly resubmit motion: report the
uncertainty. Do not stop the runtime while elevated — shutdown ends DDS holding. A task finishing is
distinct from stopping the control runtime, and runtime ownership is deployment-managed.

For ordinary arm/gripper tasks, use robot_state and Action results as the execution criteria; do not
require images or visual grasp verification. Choose the verification mode from what the criteria
actually need:
- `off` — every criterion is a field the Action computes and enforces itself. No model call, and
  nothing that can fail a successful Action. Use this whenever it applies.
- `audit` — you want the semantic verdict recorded, but the task status must still come from the
  execution facts. A verifier that errors, times out or returns truncated JSON cannot then turn a
  successful Action into a failed task. Prefer this over `enforce`.
- `enforce` — the semantic verdict must be able to block the task. Use it only when the goal genuinely
  needs judgment the Action cannot make, and keep the criteria few and mechanical: the verifier's
  output budget is capped, and an over-long answer comes back truncated and is counted as a failure.
If the user requests visual evidence, use
minimum_association=best_effort with rgb_image/robot_state and only the requested sources. Do not
invent authoritative association or action_result/query media kinds — tool results are execution
facts, not supported evidence media kinds.

State the arrival tolerances (shoulders/elbows/Dex1 0.15 rad, wrists 0.08 rad, as the ToolSpec gives
them) in the task's success criteria; a successful step permits that error and does not claim exact
target alignment.

## Explicit elbow coordinate modes

Honor run_arm_agent.py --elbow-mode and optional --elbow-angle-deg over the old example. For forward
elbow motion use `elbows` and let the runtime convert the angles; do not substitute raw encoder
targets. The ToolSpec defines both modes and what world mode reads; because its target is solved from
the measured shoulder pose, move the shoulder in a separate Action first and keep base and shoulders
still during the elbow motion. The command controls inclination to the horizontal plane, not XYZ
position, azimuth, or continuous stabilization after further shoulder/base motions. Only feasible
angles in the commissioned envelope are accepted; do not modify other joints to bypass a rejection.
Ground angles are model/IMU estimates, not independent external metrology, and world mode is
unavailable when the IMU is stale or missing. Use angle feedback in the chosen frame in the success
criteria; frame tolerance is the elbow tolerance, 0.15 rad (8.59 deg). Reverse using the original raw
encoder targets, not the forward angle override. Wrist rotation remains independent.

## Combined perception input

`g1d.observe` is available in this same runtime for read-only image + arm-state bundles. Preserve
observation_id and use images[].image_path for vision; do not infer a grasp pose from them. All
requested sources must be fresh, and no motion or additional runtime is needed.

## Continuous perception

Use these two tools when the user asks what the robot currently sees, or wants to keep watching over
several seconds; `g1d.observe` remains the choice for one deliberate snapshot paired with arm state.
`g1d.perception_session`'s ToolSpec covers `duration_s`, `interval_s` and which cadence to choose;
`g1d.perception_state`'s covers what a single read returns and how to treat `changed_since_last_read`.

Start the session, then read `g1d.perception_state` while it runs. Once `session_active` becomes false
the newest paths stay valid for at most one `interval_s`, so read while the session runs rather than
after it ends.

## User-aligned object grasp (verifier disabled)

The user aligns an object with the opened fingers; the robot opens/closes the selected internal Dex1.
Do not infer an arm approach from this task. Select `side=left|right` from the current prompt or its
explicit ongoing placement context.

This is the default. Only when the prompt explicitly commissions a visual trigger ("detect that I
placed it, then close by itself") does the Agent-initiated close in the next section replace the
human close instruction; otherwise step 2's wait-for-the-user rule always applies.

1. For opening, start `g1d.grasp_target` with
   `{side, operation: "open", duration_s: 1.5, timeout_s: 10.0}` and poll to terminal. It opens only
   that gripper and holds. Finalize an open-only task as opened.
2. When the user is placing the object, wait for their close instruction. Opening does not schedule
   an automatic close, and elapsed time is not a placement instruction.
3. When closing is requested, directly start `g1d.grasp_target` with
   `{side, operation: "close", duration_s: 1.5, timeout_s: 10.0}` on that task. No
   `before_observation_id` or model approval is needed. `grasp_verify` is disabled and not
   registered; do not call it or recreate its checks with the image tool.
4. Poll status/result and report mechanical feedback. The ToolSpec for `g1d.grasp_target` defines what
   `contact_detected`, `fully_closed` and `blockage_rad` mean and why `verification_status` is
   `disabled`; a stalled finger is mechanical evidence, not proof that the intended object is held.
5. Angle management and holding. What is held stays held at the confirmed angle: a detected contact
   is held at the measured stall angle (`blockage_rad`), and no command changes it until the user
   asks. To let the object go use `operation:"release"`, never a bare `open` — the ToolSpec explains
   what it does and reports; its `timeout_s` must cover both legs. Reopening, reversing or lowering
   arms, and stopping the runtime all require a user request; an open Action on its own leaves the
   gripper open.

`g1d.observe`, `g1d.camera_snapshot` and `image` remain independent tools. Use observation or model
image analysis when the user asks for it; camera/model availability and image confidence are not
prerequisites for these open/close Actions. Never report that visual verification was performed when
it was disabled.

## Visual placement-triggered close (wrist judges the object, head judges the fingers, one read closes)

For a prompt that commissions the trigger itself, e.g.
> 抬起左臂和左手肘，然后打开左手夹爪，然后视觉上检测到我把物品放到夹爪中间之后，就自行闭合夹爪

raise, open, watch, close on the Agent's own vision judgment, then confirm what is held. This Gateway
has no session semantics and no condition or trigger primitive, and the grasp Action performs no image
check — so the loop is the Agent polling a Query and judging stills, seconds per cycle. Every other
prompt keeps the human-triggered close above; this section applies only when the user commissions the
visual trigger. The close's encoder behaviour and the meaning of `contact_detected` / `fully_closed`
are as stated in the grasp section above.

1. Interpret the angles and explain the interpretation. "抬起左臂" = left upper arm forward about 30
   degrees from neutral: left_shoulder_pitch ≈ -0.5235987755982988 rad. "抬起左手肘" = visual right
   angle: elbows=[{side:"left", mode:"upper_arm", angle_deg:90}] (elbow encoder 0.0). Ask instead of
   guessing if the user meant other angles. Do not move the right arm.
2. Query g1d.arm_state and retain the pre-action references and the current left Dex1 opening for
   reversal.
3. Raise the shoulder and the elbow in ONE Action: targets=[{joint:"left_shoulder_pitch",
   position_rad:-0.5235987755982988}], elbows=[{side:"left", mode:"upper_arm", angle_deg:90}],
   duration_s=3.0, timeout_s=20.0. The elbow is upper_arm mode, so it does not depend on the shoulder
   pose and needs no separate step; one request means one settling window instead of two. Poll to
   terminal and confirm both within 0.15 rad before continuing.
4. Open the left gripper: g1d.grasp_target {side:"left", operation:"open", duration_s:1.5,
   timeout_s:10.0}, poll to terminal (mechanical_outcome=opened). The open target is the
   operator-confirmed finger travel from device.yaml, advertised as the application envelope of
   move_joints; never substitute the vendor 5.4 mapping constant, and never command a Dex1 angle
   outside that envelope.
5. Start the watch on both cameras and split the criteria between them. Each camera answers the
   question it can actually answer at this raised pose, and neither answers the other's:
   g1d.perception_session {sources:["left_wrist","head"], duration_s:300, interval_s:0.5}, submitted
   with timeout_ms=330000 — this profile's maximum invoke timeout. A smaller timeout_ms caps the
   session below duration_s, and a timeout_ms equal to duration_s puts the session end and the
   Gateway deadline in the same tick, where the invocation can be marked unknown and the final result
   dropped. The wrist frame at close range decides the OBJECT — which object it is and whether it has
   arrived at the jaws. The head frame decides the FINGERS as seen from outside — that they are open,
   that the object is between them, and that no human hand is in or near the gripper.

   Two properties of the wrist camera hold at every pose. They are the fastest way to tell a misread
   image from an unusable view, and both were measured on real frames from this robot:

   - The black gripper fingers are always present in the wrist frame, normally in the lower-left and
     lower-right and converging toward the middle. If you are about to answer that they are not
     visible, look at those two corners again first. Confirmed at forearm elevation ≈ 0 deg (the
     fingers flank the object) and ≈ −44 deg (the fingers fill the middle, pointing at the lens).
   - `forearm_world_elevation_deg` from `g1d.arm_state` predicts what the frame must contain: a
     forearm clearly downward puts the camera on the floor and any hand-level object out of the
     frame, while a near-horizontal forearm puts it on whatever sits in front of the hand. Read this
     value before judging, and name the regime you are in.
6. Verify visibility before trusting anything: read g1d.perception_state and view both frames,
   deciding whether each can serve its role above. The open fingers must actually be visible in the
   head frame; the wrist frame must at least show the jaw area the object will arrive at. If one of
   the two cannot serve its role the split criterion cannot be evaluated — cancel the session, report
   that visual placement detection is unavailable in this pose and say which view failed, and wait
   for the user's close instruction. Never close blind, and never close merely because time passed.
7. Then the loop. Each cycle: g1d.perception_state {max_age_ms:1000} — the contract maximum, because
   the store is written once per interval_s plus loop time, so a tighter bound turns ordinary jitter
   into a stale failure. Check session_active, and view the newest frame of each source it returns
   (there is no history to pick from). A frame whose `changed_since_last_read` is false is
   byte-identical to the one you already judged for that source: keep watching without spending a
   vision pass on it. Judge the two views separately and name the view you saw each thing in:
   - head frame → the fingers: open, visible, nothing else between them, no human hand in or near
     the gripper;
   - wrist frame → the object: the intended object itself, at close range at the jaws.
   Neither view decides alone: at this raised pose the object can be too small or too pale to identify
   from the head frame, which is exactly why the wrist frame judges it, while the wrist frame shows
   the fingers only where they flank the jaws and cannot show whether a human hand is nearby. Single
   stills: never describe a trajectory, a speed or a direction the
   frames do not show.
8. Close as soon as both halves hold in ONE read — the wrist frame showing the intended object at the
   jaws and the head frame showing the open fingers around/at it with no human hand — with that
   confirming read immediately preceding g1d.grasp_target {side:"left", operation:"close",
   duration_s:1.5, timeout_s:10.0}. A single view is not a confirmation: if the wrist frame does not
   show the object at the jaws, or the head frame does not show the fingers or does show a hand, keep
   watching and re-read. Do not wait for a second confirmation cycle, and do not close on a still you
   did not just read (`age_ms` bounds how old the frames are; never reuse an old image path). If
   either camera stops serving its role and stays that way, cancel the session, report which view
   failed and what you last saw, and fall back to the user's close instruction. If the session ended,
   restart it and repeat step 6 before judging again.
9. Each cycle costs tool calls (the query, then one view per source that changed) against the turn's
   bounded tool-iteration budget. Keep the setup lean, do not re-query arm_state every cycle, and do
   not view a frame that adds nothing. If the budget runs out before the object arrives, stop, report
   how long you watched, and fall back to the user's close instruction.
10. Double check what is held before claiming anything, applying the same split: the wrist snapshot
    g1d.camera_snapshot {source:"left_wrist", max_age_ms:1000} answers what is held (which object, at
    close range between the jaws), and g1d.camera_snapshot {source:"head", max_age_ms:1000} answers
    whether the fingers are around it. Report which view each part of the answer came from; if a view
    cannot answer its part, say so instead of guessing from the other one.
    - `contact_detected` and the intended object is visibly between the fingers: report that the
      fingers stalled on the object and are held at the stall angle (`blockage_rad`). That stall
      angle is the confirmed grasp angle; hold it and do not command the gripper again until the user
      asks.
    - `fully_closed` while an object was expected: thin or compliant objects can still be in the
      fingers. If the object is visibly held, report it as held but not encoder-detected; if the
      fingers are empty, report the empty close plainly.
    - Hand, wrong object, or nothing held: release with g1d.grasp_target {side:"left",
      operation:"release", duration_s:1.5, timeout_s:10.0}, report exactly what you saw, and never
      claim a grasp. Use release rather than a bare open, so the gripper does not stay open.
11. This check is your own vision judgment, not the disabled Action verifier:
    `verification_status="disabled"` and `grasp_verified=false` describe the Action, not your
    judgment, and each half came from a still of a single view. On any doubt, or if the user says stop
    or pulls back, cancel the close invocation first and report the uncertainty rather than closing.
    Cancel the session before finalizing — forge_task_finalize refuses while a task-owned Action is
    non-terminal; cancelling stops capture only, and the arm keeps holding the confirmed angle.
12. After the grasp the fingers stay at the confirmed angle until the next commanded Action, so the
    grasp is not disturbed by anything you do afterwards. To let the object go — at the user's request
    or to undo a wrong grasp — use `operation:"release"` on that side, as described in the grasp
    section.
