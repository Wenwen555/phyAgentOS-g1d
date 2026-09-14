#!/usr/bin/env python3
"""Build G1-D node and native Skill bundles, using the Core packager and validators."""

import argparse
import gzip
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "PhyAgentOS-core"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps/forge-gateway/src"), str(CORE)]

from paos_g1d.contracts import JOINTS, SOURCES, TOOLS, DeviceSettings  # noqa: E402
from paos_g1d.manipulation.arm import ARM_TOOLS, envelope_limits  # noqa: E402
from paos_g1d.manipulation.grasp_action import GRASP_TOOLS  # noqa: E402
from paos_g1d.observe import OBSERVE_TOOLS  # noqa: E402
from paos_g1d.perception.contracts import PERCEPTION_TOOLS  # noqa: E402

GATEWAY_LOCK = {
    "artifact_id": "gateway-1.0.2-linux-x86_64-tar-gz",
    "version": "1.0.2",
    "platform": "linux",
    "arch": "x86_64",
    "artifact_type": "executable_tar_gz",
    "entrypoint": "gateway",
    "sha256": "07584846f16012c6137b10cf743073cd2fe9a7f64564ba2322a7090e66fd3076",
}


def command(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))


def archive_node(binary, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = binary.read_bytes()
    with destination.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                info = tarfile.TarInfo("g1d_node")
                info.size, info.mode, info.mtime = len(data), 0o755, 0
                tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def gateway_config(lift, settings, *, mock, arm=False, perception=False):
    specs = []
    for tool_id, (endpoint_id, operation, semantics, request, result) in {
        **TOOLS,
        **OBSERVE_TOOLS,
        **(PERCEPTION_TOOLS if perception else {}),
        **(ARM_TOOLS if arm else {}),
        **(GRASP_TOOLS if arm else {}),
    }.items():
        if tool_id == "g1d.set_height" and not lift:
            continue
        schema = request.model_json_schema()
        if tool_id == "g1d.set_height":
            low = 0.0 if mock else settings.height.min_height_m
            high = 1.0 if mock else settings.height.max_height_m
            if low is not None and high is not None:
                schema["properties"]["target_height_m"].update(minimum=low, maximum=high)
            schema["properties"]["timeout_s"]["maximum"] = (
                15.0 if mock else settings.height.max_duration_s
            )
        specs.append(
            {
                "tool_id": tool_id,
                "implementation_id": "unitree.g1d",
                "endpoint_id": endpoint_id,
                "operation": operation,
                "semantics": semantics,
                "description": {
                    "g1d.grasp_target": "Open, close or release the selected internal Dex1 for a user-aligned object. Required side=left|right follows the current prompt. operation=open opens only the selected Dex1 to the operator-confirmed finger travel and holds until a separate close instruction; this tool never schedules an automatic close, and a user placement is not a close instruction on its own. operation=close closes towards 0 while watching the encoder: a finger stopped by an object settles above the confirmed empty-close rest position, and that stall is reported as contact_detected with the measured blockage_rad, after which the fingers are held at the stall angle instead of pressing on - that held angle is the confirmed grasp angle. fully_closed means the fingers reached the empty-close rest position: the encoder saw no object in the way, which is inconclusive for a thin or compliant object. operation=release opens to the same travel, then closes again by itself, so the gripper is never left open after letting an object go: it reports released_then_closed when the fingers came back to the closed rest position and contact_detected when something - typically the object - is still between them. Visual verifier is disabled: no before_observation_id, image capture, model calls or visual gate. Returns mechanical_outcome and verification_status=disabled, grasp_verified=false; contact_detected is mechanical evidence, not proof that the intended object is held. Independent observation remains available when requested. No arm approach or IK. Holds after completion/cancel. Shares exclusive arm control and retains feedback freshness, speed limits and deadlines.",
                    "g1d.observe": "Read-only observation bundle: requested camera JPEGs plus fresh arm/Dex1 state and torso IMU. Returns immutable observation_id/manifest_path, image paths/hashes, ages and conservative host receive-time skew. sources defaults to [head]; max_age_ms=500, max_skew_ms=100. All requested sources must be fresh and sufficiently aligned. Best-effort receive-time association, not synchronized sensor capture. No detection, depth or calibrated 3D positions. No column-height prerequisite.",
                    "g1d.arm_state": "Read fresh arm and internal Dex1 encoder angles, references, phase, effective duration, torso IMU RPY/age, elbow bend and forearm ground elevation in degrees. Radians; reference_held denotes ongoing controller ownership, not a brake acknowledgment. Optional joints=[...] returns only those axes so a caller stops carrying the full 16-joint pose through its context; freshness is still checked over the complete pose. Omit joints for the full detail.",
                    "g1d.move_joints": "Move raw targets to encoder radians, or use elbows=[{side,mode,angle_deg}]. upper_arm bend: straight=0, bent=90 degrees. world forearm elevation: down=-90, horizontal=0, up=90 degrees; uses fresh torso IMU and measured 3D shoulder pose. Move the shoulder in an earlier Action only for world-mode elbows; upper_arm-mode elbows are independent of the shoulder and belong in the same request as it. Unreachable angles are rejected; world mode is a one-shot elevation target, not persistent stabilization or Cartesian IK. Quintic trajectory; unspecified joints retain their last references. Batch every joint that can move together into one request: a single call applies one trajectory and one settling window to the whole set, so a whole pose costs one Action instead of a chain of single-joint steps. Visual elbow bend 90 degrees means elbow encoder 0 in upper_arm mode, irrespective of shoulder pitch, and does not mean the forearm must stay world-horizontal; a hanging elbow is pi/2. Left wrist roll is rotation about forearm axis. Internal left/right Dex1 are motors 31/33: 0 closed; the open target is the operator-confirmed finger travel inside the application envelope below, because the vendor mapping constant 5.4 can exceed the real finger travel and an unreachable target stalls without failing. Shoulder/elbow/Dex1 tolerance 0.15 rad; wrist joints 0.08. Controller holds after completion. Cancel freezes reference, not reverse recovery or proof of physical stopping. Application envelopes: "
                    + str(envelope_limits(settings.dex1, mock=mock)),
                    "g1d.state": "Read fresh G1-D column height in metres and joint positions/velocities in radians and rad/s. Optional joints=[...] returns only those axes; joints=[] reads the column height alone, which is all a lift step needs.",
                    "g1d.camera_snapshot": "Save a fresh camera JPEG on the Agent host; return image_path for the native image tool. Head is one stereo pair: two 640x480 eyes side by side in a 1280x480 frame.",
                    "g1d.perception_state": "Read the newest frame captured by g1d.perception_session: exactly one frame per requested source, and no earlier frames. Every entry carries image_path, sha256, sequence, receive time and age_ms measured at read time - compare ages to judge how stale a frame is. Each entry also carries changed_since_last_read: false means that source's frame is byte-identical to the one returned by your previous successful read, so you have already judged it and can skip the vision pass; a first read, a new source and a read after a stale failure all report true. Use the native image tool on that path. Each read returns the frame current at that moment, so a caller watching for a change has to read again and compare its own observations: the store keeps no history and a still is not video - never infer a continuous trajectory, speed or direction from two stills. Native JPEGs only: no detection, depth or calibrated 3D position, and image pixels are not robot target coordinates. Fails when no session has run recently or the stored state is older than max_age_ms.",
                    "g1d.perception_session": "Continuously capture native camera JPEGs for the Agent's own vision model, so the robot can report what it currently sees. Runs for duration_s seconds (default 30, at most 300) at interval_s cadence (default 0.5), then ends by itself; cancel stops it earlier. Choose interval_s from the task: 2.0 for a glance, 0.5 to watch a scene, 0.1-0.2 while the arm moves or the moment of contact matters, since interval_s is how often the newest frame g1d.perception_state returns is replaced. Frames are written to unique content-addressed paths on the same host filesystem as the Agent. No detection, tracking, depth or 3D localization is performed, and no robot motion is commanded. This Action never judges or triggers anything: a placement-watch loop is composed by the Agent polling g1d.perception_state and acting on its own vision judgment. Read results with g1d.perception_state; progress events report captured frame counts. Pass timeout_ms above duration_s (this profile allows up to 330000) or the Gateway deadline caps the run.",
                    "g1d.set_height": "Move only the G1-D column to an absolute height in the rt/hispeed_state Point32.y coordinate, in metres. Bounded velocity command, deadline, settling and fresh stop observation. Read Endpoint status for enabled flag and confirmed limits.",
                }[tool_id],
                "input_schema": schema,
                "output_schema": result.model_json_schema(),
                "readiness": [],
                "robot_frame_profile": {
                    "robot_id": "g1d-mock" if mock else "g1d",
                    "base_frame": "torso" if tool_id in ARM_TOOLS or tool_id in GRASP_TOOLS else "column_origin",
                    "tool_frame": "joint_encoder" if tool_id in ARM_TOOLS or tool_id in GRASP_TOOLS else "column",
                    "frames": {},
                },
            }
        )
    routes = [("state", "g1d.state"), ("camera", "g1d.cameras"), ("observe", "g1d.observe")]
    if perception:
        routes += [
            ("perception_state", "g1d.perception_state"),
            ("perception_session", "g1d.perception_session"),
        ]
    if arm:
        routes += [("arm_state", "g1d.arm_state"), ("arm", "g1d.arm"), ("grasp", "g1d.grasp")]
    if lift:
        routes.append(("lift", "g1d.lift"))
    return {
        "host": "127.0.0.1",
        "port": 19002,
        "joint_order": [*JOINTS, "lift_column"],
        "image_input_ids": list(SOURCES),
        "state_broadcast_hz": 20,
        "image_broadcast_hz": 10,
        "readiness": {"require_proprio_state": False, "require_images": False},
        "agent": {"enabled": False, "action_manifests": []},
        "tools": {
            "enabled": True,
            "lease_ttl_ms": 3000,
            # Headroom above the longest Action a caller may request (perception_session,
            # 300 s): with equal values the Gateway exchange deadline expires a message
            # hop before the session ends and marks the invocation ambiguous, dropping the
            # final result. Other Actions stay bounded by their own timeout_s.
            "invoke_timeout_ms": 330000,
            "request_input_id": "tool_request",
            "response_output_id": "tool_response",
            "specs": specs,
            "providers": [
                {
                    "endpoint_id": endpoint,
                    "input_id": route + "_tool_in",
                    "output_id": route + "_tool_out",
                }
                for route, endpoint in routes
            ],
        },
    }


def dataflow(lift, *, mock, arm=False, perception=False):
    # Tool messages include admission, status, result and lease traffic. Keep a FIFO
    # on both ends: Dora's default latest-value queue can drop an Action admission
    # when the Core client immediately starts polling status/result.
    routes = (
        ["state", "camera", "observe"]
        + (["perception_state", "perception_session"] if perception else [])
        + (["lift"] if lift else [])
        + (["arm_state", "arm", "grasp"] if arm else [])
    )
    args = (
        "--backend mock"
        if mock
        else "--backend unitree --device device.yaml --sdk-library ${PAOS_SKILL_ROOT}/assets/native/libg1d_sdk.so"
    )
    args += " --cameras ${PAOS_SKILL_ROOT}/assets/cameras.yaml"
    if lift:
        args += " --allow-height"
    if arm:
        args += " --allow-arm"
    if perception:
        args += " --allow-perception"
    return {
        "nodes": [
            {
                "id": "gateway",
                "path": "${FORGE_RUNTIME_BIN}/gateway",
                "args": "--config gateway.yaml",
                "inputs": {
                    "tick": "dora/timer/millis/50",
                    "proprio_state": "g1d/state",
                    **{source: "g1d/" + source for source in SOURCES},
                    **{
                        route + "_tool_in": {
                            "source": "g1d/" + route + "_tool_out",
                            "queue_size": 256,
                        }
                        for route in routes
                    },
                },
                "outputs": ["tool_response", *[route + "_tool_out" for route in routes]],
            },
            {
                "id": "g1d",
                "path": "${FORGE_RUNTIME_BIN}/g1d_node",
                "args": args,
                "inputs": {
                    "tick": "dora/timer/millis/50",
                    **{
                        route + "_tool_in": {
                            "source": "gateway/" + route + "_tool_out",
                            "queue_size": 256,
                        }
                        for route in routes
                    },
                },
                "outputs": ["state", *SOURCES, *[route + "_tool_out" for route in routes]],
            },
        ]
    }


def build(skip_binary=False):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("the verified upstream Gateway lock currently targets Linux x86_64")
    settings = DeviceSettings.model_validate(
        yaml.safe_load((ROOT / "configs/device.yaml").read_text())
    )
    network = yaml.safe_load((ROOT / "configs/network.yaml").read_text())
    if settings.network_interface != network["interface"]:
        raise ValueError("device.yaml and network.yaml disagree on the communication interface")
    if not skip_binary:
        command("cmake", "-S", "native", "-B", "build/native", "-DCMAKE_BUILD_TYPE=Release")
        command("cmake", "--build", "build/native", "--parallel", "2")
        command(
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "g1d_node",
            "--distpath",
            "build/bin",
            "--workpath",
            "build/pyinstaller",
            "--specpath",
            "build",
            "--paths",
            "src",
            "--paths",
            ".deps/forge-gateway/src",
            "--collect-all",
            "dora",
            "--collect-all",
            "forge_msgs",
            "scripts/node_entry.py",
        )
    archive = ROOT / "dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz"
    digest = archive_node(ROOT / "build/bin/g1d_node", archive)
    lock = {
        "artifact_id": "g1d-node-0.1.0-" + digest[:16],
        "version": "0.1.0",
        "platform": "linux",
        "arch": "x86_64",
        "artifact_type": "executable_tar_gz",
        "entrypoint": "g1d_node",
        "sha256": digest,
    }
    # Reuse Core's own packager, archive validation, and manifest parser.
    spec = importlib.util.spec_from_file_location(
        "paos_package_skill", CORE / "scripts/package_skill.py"
    )
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    from forge_gateway.config import GatewayConfig
    from PhyAgentOS.skill_runtime.manifest import load_manifest

    outputs = []
    for name, lift, arm, perception in (
        ("g1d-observe", False, False, False),
        ("g1d-lift", True, False, False),
        ("g1d-robot", False, True, True),
    ):
        root = ROOT / "skills" / name
        assets = root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "configs/cameras.yaml", assets / "cameras.yaml")
        native = assets / "native"
        native.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "build/native/libg1d_sdk.so", native / "libg1d_sdk.so")
        for library in ("libddsc.so.0", "libddscxx.so.0"):
            shutil.copyfile(
                ROOT.parent / "unitree_sdk2-main/thirdparty/lib/x86_64" / library, native / library
            )
        shutil.copyfile(ROOT / ".deps/forge-gateway/LICENSE", assets / "LICENSE.forge-gateway")
        shutil.copyfile(ROOT / ".deps/forge-gateway/NOTICE", assets / "NOTICE.forge-gateway")
        shutil.copyfile(ROOT.parent / "unitree_sdk2-main/LICENSE", assets / "LICENSE.unitree-sdk2")
        for project in (
            "eclipse-cyclonedds/cyclonedds",
            "eclipse-cyclonedds/cyclonedds-cxx",
            "eclipse-iceoryx/iceoryx",
            "Tencent/rapidjson",
        ):
            shutil.copyfile(
                ROOT.parent / "unitree_sdk2-main/licenses" / project / "LICENSE",
                assets / ("LICENSE." + project.rsplit("/", 1)[-1]),
            )
        shutil.copyfile(ROOT / "docs/third-party.md", root / "THIRD_PARTY_NOTICES.md")
        # Preserve license files from the environment used to freeze the executable.
        for distribution in importlib.metadata.distributions():
            for relative in distribution.files or ():
                basename = Path(relative).name.upper()
                if basename.startswith(("LICENSE", "COPYING", "NOTICE")):
                    source = Path(distribution.locate_file(relative))
                    if source.is_file():
                        directory = assets / "python-licenses" / distribution.metadata["Name"]
                        directory.mkdir(parents=True, exist_ok=True)
                        license_filename = (
                            hashlib.sha256(str(relative).encode()).hexdigest()[:8]
                            + "-"
                            + Path(relative).name
                        )
                        shutil.copyfile(source, directory / license_filename)
        profiles = {}
        for profile, mock in (("mock", True), ("real", False)):
            directory = root / "profiles" / profile
            gateway = gateway_config(lift, settings, mock=mock, arm=arm, perception=perception)
            GatewayConfig.from_dict(gateway)
            dump(directory / "gateway.yaml", gateway)
            dump(
                directory / "dataflow.yaml",
                dataflow(lift, mock=mock, arm=arm, perception=perception),
            )
            profile_settings = settings.model_dump()
            if not lift:
                profile_settings["height"]["enabled"] = False
            dump(directory / "device.yaml", profile_settings)
            profiles[profile] = {
                "dataflow": f"profiles/{profile}/dataflow.yaml",
                "required_binaries": ["gateway", "g1d_node"],
                "required_assets": [
                    "assets/cameras.yaml",
                    *["assets/native/" + p.name for p in sorted(native.iterdir())],
                ],
                "required_environment": ["PAOS_G1D_SNAPSHOT_DIR"],
                "environment": {},
            }
        dump(
            root / "skill.yaml",
            {
                "manifest_version": 2,
                "name": name,
                "version": "0.1.22" if arm else "0.1.1",
                "description": "G1-D state and camera observation"
                + (
                    " with parameterized joint control"
                    if arm
                    else " with bounded column height control"
                    if lift
                    else " without motion"
                ),
                "skill_document": "SKILL.md",
                "gateway_url": "http://127.0.0.1:19002",
                "required_tools": (list(TOOLS) if lift else list(TOOLS)[:2])
                + list(OBSERVE_TOOLS)
                + (list(PERCEPTION_TOOLS) if perception else [])
                + (list(ARM_TOOLS) + list(GRASP_TOOLS) if arm else []),
                "profiles": profiles,
                "artifacts": {"resolver": "local", "nodes": {"gateway": GATEWAY_LOCK, "g1d": lock}},
            },
        )
        load_manifest(root / "skill.yaml")
        bundle = packager.package(root, ROOT / "dist/skills", force=True)
        outputs.append(
            {
                "path": str(bundle.relative_to(ROOT)),
                "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "size": bundle.stat().st_size,
            }
        )
    (ROOT / "dist/build-report.json").write_text(
        json.dumps({"gateway": GATEWAY_LOCK, "g1d": lock, "bundles": outputs}, indent=2)
    )
    print(json.dumps(outputs, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-binary",
        action="store_true",
        help="Only regenerate profiles/archives from an existing node build",
    )
    build(parser.parse_args().skip_binary)
