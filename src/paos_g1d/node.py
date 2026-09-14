"""Dora node: upstream Forge Tool binding plus G1-D cameras/state/height operations."""

import argparse
import asyncio
import faulthandler
import logging
import os
import signal
import time
import uuid
from pathlib import Path

import yaml
from forge_msgs import CompressedImage, JointState
from forge_tool import (
    DEFAULT_MAX_MESSAGE_BYTES,
    TOOL_ENDPOINT_PROTOCOL,
    EndpointStatus,
    ToolEndpointDescriptor,
    ToolEndpointHandler,
    ToolOperationDescriptor,
    make_endpoint_status_envelope,
    make_registration_envelope,
)
from forge_tool._tool_message import ToolMessage
from forge_tool.dora import (
    DoraToolEndpointBinding,
    tool_envelope_to_message,
    tool_message_to_envelope,
)

from .cameras import Cameras
from .contracts import JOINTS, SOURCES, DeviceSettings, HeightSettings
from .device import MockDevice, UnitreeDevice
from .endpoints import LiftAction, SnapshotQuery, StateQuery, fresh_height
from .manipulation.arm import MockArm, NativeArm, configure_limits
from .manipulation.arm_endpoint import ArmQuery, JointAction
from .manipulation.grasp_action import GraspAction
from .observe import ObserveQuery
from .perception.session_endpoint import PerceptionSession, PerceptionStateQuery
from .perception.state_store import StateStore


class Endpoints:
    """Dora I/O adapter; wire messages and executor routing come from forge_tool."""

    def __init__(
        self,
        device,
        cameras,
        settings,
        publish,
        *,
        allow_height=False,
        allow_arm=False,
        allow_perception=False,
    ):
        self.device, self.cameras, self.settings = device, cameras, settings
        self.publish = publish
        self.lift = LiftAction(device, settings.height)
        self.arm = MockArm() if device.simulated else NativeArm(device)
        self.arm_action = JointAction(self.arm, allow_arm)
        self.grasp_action = GraspAction(self.arm_action, settings.dex1)
        self.perception_store = StateStore()
        self.perception_session = PerceptionSession(cameras, self.perception_store)
        self.instance = str(uuid.uuid4())
        implementations = {
            "state": ("g1d.state", "get", "query", StateQuery(device)),
            "camera": ("g1d.cameras", "snapshot", "query", SnapshotQuery(cameras)),
            "observe": ("g1d.observe", "get", "query", ObserveQuery(cameras, self.arm)),
        }
        if allow_height:
            implementations["lift"] = ("g1d.lift", "set_height", "action", self.lift)
        if allow_perception:
            # Only the profile that declares these routes may publish on them.
            implementations["perception_state"] = (
                "g1d.perception_state",
                "get",
                "query",
                PerceptionStateQuery(self.perception_store),
            )
            implementations["perception_session"] = (
                "g1d.perception_session",
                "run",
                "action",
                self.perception_session,
            )
        if allow_arm:
            implementations["arm_state"] = ("g1d.arm_state", "get", "query", ArmQuery(self.arm))
            implementations["arm"] = ("g1d.arm", "move", "action", self.arm_action)
            implementations["grasp"] = ("g1d.grasp", "execute", "action", self.grasp_action)
        self.bindings = {}
        for route, (endpoint_id, operation, semantics, implementation) in implementations.items():
            descriptor = ToolEndpointDescriptor(
                protocol_version=TOOL_ENDPOINT_PROTOCOL,
                endpoint_id=endpoint_id,
                operations=(
                    ToolOperationDescriptor(
                        name=operation,
                        semantics=semantics,
                        cancellable=semantics == "action",
                        status_supported=semantics == "action",
                        max_concurrency=1,
                    ),
                ),
            )
            handler = ToolEndpointHandler(
                descriptor,
                endpoint_instance_id=self.instance,
                operations={operation: implementation},
            )

            async def sink(value, output=route + "_tool_out"):
                self.publish(output, value)

            self.bindings[route] = DoraToolEndpointBinding(handler, event_sink=sink)
        self.last_announce = -float("inf")
        self.last_state = (0, 0)
        self.last_registry_ack = None
        self.lift.link_ok = lambda: (
            self.last_registry_ack is not None and time.monotonic() - self.last_registry_ack <= 2.0
        )

        self.arm_action.link_ok = self.lift.link_ok

        self.perception_session.link_ok = self.lift.link_ok

    async def handle(self, input_id, value):
        route = input_id.removesuffix("_tool_in")
        binding = self.bindings.get(route)
        if binding is None:
            return
        if not hasattr(value, "nbytes") or value.nbytes > DEFAULT_MAX_MESSAGE_BYTES + 65536:
            raise ValueError("Tool Arrow carrier exceeds the protocol limit")
        envelope = tool_message_to_envelope(
            ToolMessage.from_arrow(value, max_payload_json_bytes=DEFAULT_MAX_MESSAGE_BYTES)
        )
        if envelope.message_type == "endpoint.registry.response":
            if (
                envelope.endpoint_instance_id == self.instance
                and envelope.payload.get("status") == "accepted"
            ):
                self.last_registry_ack = time.monotonic()
            return
        for response in await binding.dispatch_input(value):
            self.publish(route + "_tool_out", response)

    async def tick(self):
        now = time.monotonic()
        # Losing the Gateway lease path during a goal requests a local stop as well.
        if self.lift.active is not None and (
            self.last_registry_ack is None or now - self.last_registry_ack > 2.0
        ):
            await self.lift.cancel(self.lift.active, "Gateway registration acknowledgements lost")
        # Receive on the loop (ZeroMQ sockets stay on their creating thread), decode in
        # a worker: PIL work on three 30 fps streams would otherwise stall the Action
        # control loops that share this event loop.
        payloads = self.cameras.drain()
        changed = await asyncio.to_thread(self.cameras.ingest_all, payloads) if payloads else []
        for source in changed:
            frame = self.cameras.frames[source]
            self.publish(source, CompressedImage(format="jpeg", data=frame.jpeg).to_arrow())
        state = await asyncio.to_thread(self.device.read)
        sequence = (state.height_sequence, state.joint_sequence)
        if (
            sequence != self.last_state
            and fresh_height(state, 0.3)
            and 0 <= state.joint_age_s <= 0.3
            and state.joint_sequence
        ):
            self.publish(
                "state",
                JointState(
                    name=[*JOINTS, "lift_column"], position=[*state.positions, state.height]
                ).to_arrow(),
            )
            self.last_state = sequence
        if now - self.last_announce < 0.5:
            return
        self.last_announce = now
        camera_health = {}
        for source in SOURCES:
            try:
                self.cameras.frame(source, self.settings.camera_max_age_s)
                camera_health[source] = "ready"
            except ValueError as error:
                camera_health[source] = str(error)
        for route, binding in self.bindings.items():
            handler = binding.handler
            registration = make_registration_envelope(
                handler.descriptor, endpoint_instance_id=self.instance, request_id=str(uuid.uuid4())
            )
            self.publish(route + "_tool_out", tool_envelope_to_message(registration).to_arrow())
            if route == "state":
                ready = (
                    fresh_height(state, 0.3)
                    and state.joint_sequence > 0
                    and 0 <= state.joint_age_s <= 0.3
                )
                details = {
                    "time_basis": "device_receive_monotonic",
                    "simulated": self.device.simulated,
                }
            elif route == "camera":
                ready = all(value == "ready" for value in camera_health.values())
                details = {"sources": camera_health, "simulated": self.device.simulated}
            elif route == "observe":
                observed_arm = await asyncio.to_thread(self.arm.read)
                ready = 0 <= observed_arm.age_s <= 0.1 and any(
                    value == "ready" for value in camera_health.values()
                )
                details = {
                    "sources": camera_health,
                    "simulated": self.device.simulated,
                    "association": "best_effort_receive_time",
                }
            elif route in ("perception_state", "perception_session"):
                ready = all(value == "ready" for value in camera_health.values())
                details = {
                    "sources": camera_health,
                    "simulated": self.device.simulated,
                    "store_age_ms": (
                        (time.monotonic() - self.perception_store.updated) * 1000
                        if self.perception_store.updated is not None
                        else None
                    ),
                    "session_active": self.perception_session.active is not None,
                }
            elif route in ("arm", "arm_state", "grasp"):
                arm_state = await asyncio.to_thread(self.arm.read)
                ready = 0 <= arm_state.age_s <= 0.1 and not self.arm_action.faulted
                details = arm_state.model_dump()
                if route == "grasp":
                    details["verification_status"] = "disabled"
                    details["requires_object_aligned"] = True
            else:
                ready = (
                    self.settings.height.enabled
                    and fresh_height(state, 0.3)
                    and not state.watchdog_tripped
                    and not self.lift.faulted
                )
                details = {
                    "enabled": self.settings.height.enabled,
                    "limits": self.settings.height.model_dump(),
                    "simulated": self.device.simulated,
                }
            active = int(
                (route == "lift" and self.lift.active is not None)
                or (route == "arm" and self.arm_action.active is not None)
                or (route == "grasp" and self.grasp_action.active is not None)
                or (route == "perception_session" and self.perception_session.active is not None)
            )
            status = EndpointStatus(
                endpoint_id=handler.descriptor.endpoint_id,
                state="ready" if ready else "unavailable",
                active_invocations=active,
                details=details,
            )
            self.publish(
                route + "_tool_out",
                tool_envelope_to_message(
                    make_endpoint_status_envelope(status, endpoint_instance_id=self.instance)
                ).to_arrow(),
            )

    async def close(self):
        await self.grasp_action.close()
        await self.perception_session.close()
        await self.arm_action.close()
        await self.lift.close()
        self.cameras.close()
        self.device.close()


async def run(args):
    from dora import Node

    snapshot_dir = Path(os.environ["PAOS_G1D_SNAPSHOT_DIR"])
    if not snapshot_dir.is_absolute():
        raise ValueError("PAOS_G1D_SNAPSHOT_DIR must be an absolute path shared with the Agent")
    config = yaml.safe_load(Path(args.cameras).read_text())
    if args.backend == "mock":
        settings = DeviceSettings(
            network_interface="mock",
            height=HeightSettings(
                enabled=args.allow_height,
                limits_confirmed=True,
                min_height_m=0.0,
                max_height_m=1.0,
            ),
        )
        device = MockDevice()
    else:
        settings = DeviceSettings.model_validate(yaml.safe_load(Path(args.device).read_text()))
        if not args.allow_height:
            settings.height.enabled = False
        # Narrow the commandable envelopes to the operator-confirmed Dex1 travel
        # before any request is parsed; the mock runtime keeps the defaults.
        configure_limits(settings.dex1)
        device = UnitreeDevice(Path(args.sdk_library), settings)
    cameras = Cameras(config, settings, snapshot_dir, simulated=device.simulated)
    endpoints = None
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        node = Node()
        endpoints = Endpoints(
            device,
            cameras,
            settings,
            node.send_output,
            allow_height=args.allow_height,
            allow_arm=args.allow_arm,
            allow_perception=args.allow_perception,
        )
        cameras.open()
        while not stopped.is_set():
            # Dora 0.4.1's experimental recv_async stalled at its Rust/Python callback
            # boundary in sustained runs. next() releases the GIL; run its bounded
            # receive in a worker so Action/cancellation tasks keep progressing.
            event = await asyncio.to_thread(node.next, 0.5)
            if event is None or event["type"] == "STOP":
                break
            if event["type"] == "INPUT":
                if event["id"] == "tick":
                    await endpoints.tick()
                else:
                    await endpoints.handle(event["id"], event["value"])
            elif event["type"] == "ERROR":
                raise RuntimeError(str(event))
            await asyncio.sleep(0)
    finally:
        if endpoints is not None:
            await endpoints.close()
        else:
            cameras.close()
            device.close()


def main():
    # Allow a local, non-motion SIGUSR1 diagnostic when the event loop is blocked.
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("mock", "unitree"), required=True)
    parser.add_argument("--cameras", required=True)
    parser.add_argument("--device")
    parser.add_argument("--sdk-library")
    parser.add_argument("--allow-height", action="store_true")
    parser.add_argument("--allow-arm", action="store_true")
    parser.add_argument("--allow-perception", action="store_true")
    args = parser.parse_args()
    if args.backend == "unitree" and (not args.device or not args.sdk_library):
        parser.error("unitree requires --device and --sdk-library")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
