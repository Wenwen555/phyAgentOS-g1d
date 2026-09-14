import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from forge_tool.wire.messages import tool_result_to_payload
from pydantic import ValidationError

from paos_g1d.manipulation.arm import MockArm
from paos_g1d.manipulation.grasp_verifier import GraspVerifier, GraspVerifyRequest
from paos_g1d.observe import ObserveQuery


def vision(**changes):
    value = dict(
        object_visible="yes",
        between_fingers="yes",
        fingers_clear_of_people="yes",
        held_without_external_support="yes",
        confidence=0.95,
        reason="Synthetic fixture only",
    )
    value.update(changes)
    return SimpleNamespace(content=json.dumps(value))


async def precheck(cameras, arm, side, response=None):
    query = ObserveQuery(cameras, arm)

    async def observe(tool, args):
        assert tool == "g1d.observe"
        cameras.poll()
        result = await query.query(SimpleNamespace(arguments=args), None)
        return {"data": {"response": {"result": tool_result_to_payload(result)}}}

    client = SimpleNamespace(invoke_query_tool=observe, invocation_result=AsyncMock())
    provider = SimpleNamespace(chat_with_retry=AsyncMock(return_value=response or vision()))
    verifier = GraspVerifier(provider, client, cameras.snapshot_dir)
    report = await verifier.verify(
        dict(stage="before", side=side, target_description="test object")
    )
    return verifier, report


async def test_bad_model_response_cannot_authorize(cameras):
    arm = MockArm()
    arm.q["left_dex1"] = 5.4
    _, report = await precheck(cameras, arm, "left", SimpleNamespace(content="not JSON"))
    assert report["status"] == "inconclusive"
    assert report["ready_to_close"] is False



@pytest.mark.parametrize("changes", [dict(object_visible="no"), dict(confidence=0.3)])
async def test_inactive_verifier_keeps_uncertain_evidence_inconclusive(cameras, changes):
    arm = MockArm()
    arm.q["left_dex1"] = 5.4
    _, report = await precheck(cameras, arm, "left", vision(**changes))
    assert report["ready_to_close"] is False
    assert not arm.history


def test_inactive_verifier_after_requires_invocation():
    with pytest.raises(ValidationError):
        GraspVerifyRequest(stage="after", side="left", target_description="object")
