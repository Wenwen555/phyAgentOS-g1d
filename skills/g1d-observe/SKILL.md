---
name: g1d-observe
description: Observe G1-D state and head/left-wrist/right-wrist images without moving the robot.
metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["g1d-observe"]}}}
---

# G1-D observation

Activate this Skill as primary for G1-D observation and visual questions. Use only
`g1d.observe`, `g1d.state` and `g1d.camera_snapshot` through the native Forge tools.
Prefer g1d.observe for an associated image + arm-state observation.

1. Call `forge_tool_context` once for each tool you will actually invoke, and only for those.
   Check active Runtime identity, Tool schemas and Endpoint readiness.
   `simulated: true` means a local mock; never describe its data as real robot evidence.
2. For a verified user goal, create an AgentTask from this activation and bind related Queries to
   the same task. Diagnostic Queries may be unbound. Do not create an Action to obtain evidence.
3. Query `g1d.state` using an explicit `max_age_ms` permitted by context. Column height is in
   metres in the column's own `rt/hispeed_state` coordinate. Joint positions and velocities are
   radians and radians/second; this is not a world/base localization estimate.
4. Query `g1d.camera_snapshot` with a source and freshness bound. Use the returned `image_path`
   with the existing `image` tool's `vision` mode and the user's visual question. This requires
   a configured multimodal Provider and a filesystem shared by the node and Agent.
5. The head image contains both eyes side by side at equal sharpness; read either half, and
   prefer the half where the subject is less occluded. Do not infer a 3D target, calibrated
   pose or object graspability from these uncalibrated JPEGs.
6. Report missing/stale/dark images and unavailable state explicitly. Do not reuse an old snapshot
   as a current observation or turn visibility failures into robot motion.
7. Finalize a bound task with the native verification flow. Frame association is best effort;
   Gateway timestamps describe receipt, not synchronized camera capture time.

This Skill has no motion capability. Do not run vendor examples, call the SDK from the Agent,
construct a second HTTP execution route, or change Runtime/configuration to gain motion access.


## Combined observation

Call `g1d.observe` with `sources` (default ["head"]), `max_age_ms` (default 500),
`max_skew_ms` (default 100). This Query never moves the robot. It captures every requested
camera plus fresh arm/Dex1 feedback, without depending on column-height feedback. Missing,
stale, dark or excessively misaligned requested sources fail the whole observation. Unrequested
cameras do not block it. Re-query for fresh evidence; do not silently relax the caller's bounds.

Outputs include observation_id, immutable JSON manifest_path, images with image_path/sha256,
receive timestamps, robot_state, ages, read timing uncertainty and conservative receive-time skew.
Use image_path with the existing vision tool when the user requests semantic interpretation.
No object recognition is performed by observe itself. Association is best_effort_receive_time,
not hardware synchronization. Camera exposure timestamps, depth and spatial calibration are not
provided. Do not use these bundles alone as calibrated 3D grasp targets. Keep the observation_id
when passing this evidence to subsequent perception tools. Paths require the same host filesystem.
