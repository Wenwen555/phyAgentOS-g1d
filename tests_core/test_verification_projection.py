"""The projecting verifier builder must lose payload, never evidence references.

Core's `VerificationRequestBuilder` emits the task's execution records three times. The
project subclass projects that context down before it is serialized, so these tests pin the
invariant that makes the projection safe: the set of evidence-reference carriers is identical
before and after, and the success criteria are untouched.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paos_g1d.verification import (  # noqa: E402
    RECORD_FIELDS,
    TERMINAL_RESULT_FIELDS,
    project_verification_context,
)

INVOCATION = "invocation:gateway-c81add30628043c4aa045c5f44a5f8d2"
TOOL_REF = "tool:tool_06d7012f4fbe40a6"
ARTIFACT = "artifact_ee6c3e02a242b8ad552c"


def record(record_id, tool_id, status, *, evidence_refs, response=None):
    return {
        "version": "tool_execution_record_v2",
        "record_id": record_id,
        "revision_id": "revision-1",
        "tool_id": tool_id,
        "semantics": "action" if "set_height" in tool_id else "query",
        "skill_binding_id": "binding-1",
        "tool_spec_sha256": "a" * 64,
        "caller_id": "agent",
        "ownership": "task",
        "arguments": {"source": "head", "max_age_ms": 1000},
        "status": status,
        "invocation_id": f"gateway-{record_id}",
        "attempt_id": "attempt-1",
        "response": response or {"ok": True, "data": {"outputs": {"x": 1}}},
        "error": None,
        "evidence_refs": list(evidence_refs),
        "created_at": "2026-09-08T08:57:36+00:00",
        "updated_at": "2026-09-08T08:57:36+00:00",
    }


def core_context():
    """A context shaped like the one Core's `_build_request` receives, with its three copies."""
    records = [
        record("tool_1", "g1d.state", "succeeded", evidence_refs=[]),
        record("tool_2", "g1d.camera_snapshot", "succeeded", evidence_refs=[ARTIFACT]),
        record("tool_3", "g1d.set_height", "succeeded", evidence_refs=[INVOCATION, TOOL_REF]),
    ]
    return {
        "agent_task": {"task_id": "task_1", "task_description": "raise the column"},
        "goal": "Reach 0.10 m within 0.002 m",
        "criteria": ["one successful Action", "final height within tolerance"],
        "constraints": [],
        "task_verification_contract": {"mode": "audit"},
        "frozen_skill_binding": {"binding_id": "binding-1"},
        "supporting_skill_bindings": [],
        "plan_revisions": [
            {
                "revision_id": "revision-1",
                "number": 1,
                "reason": "initial plan",
                "verdict": None,
                "verification_attempts": [],
                "execution_records": records,
            }
        ],
        "tool_execution_records": records,
        "gateway_terminal_results": [
            {
                "record_id": item["record_id"],
                "tool_id": item["tool_id"],
                "semantics": item["semantics"],
                "status": item["status"],
                "invocation_id": item["invocation_id"],
                "attempt_id": item["attempt_id"],
                "response": item["response"],
                "error": None,
                "evidence_refs": item["evidence_refs"],
            }
            for item in records
        ],
        "evidence_bundle": {"artifacts": [{"artifact_id": ARTIFACT, "kind": "rgb_image"}]},
        "structured_evidence": {},
        "evidence_errors": [],
        "events": [],
        "lessons": "[]",
        "valid_evidence_refs": sorted({ARTIFACT, INVOCATION, TOOL_REF}),
    }


def carriers(context):
    """Every value that can satisfy an evidence reference, per Core's ref construction."""
    found = set()
    for item in context.get("tool_execution_records") or []:
        found.update(item.get("evidence_refs") or [])
    for artifact in (context.get("evidence_bundle") or {}).get("artifacts") or []:
        if artifact.get("artifact_id"):
            found.add(artifact["artifact_id"])
    return found


def serialized(context):
    return json.dumps(context, ensure_ascii=False)


class ProjectionTest(unittest.TestCase):
    def test_every_evidence_reference_stays_resolvable(self):
        original = core_context()
        projected = project_verification_context(original)
        self.assertEqual(carriers(projected), carriers(original))
        self.assertEqual(carriers(projected), set(original["valid_evidence_refs"]))

    def test_criteria_and_valid_refs_are_untouched(self):
        original = core_context()
        projected = project_verification_context(original)
        self.assertEqual(projected["criteria"], original["criteria"])
        self.assertEqual(projected["goal"], original["goal"])
        self.assertEqual(projected["valid_evidence_refs"], original["valid_evidence_refs"])
        self.assertEqual(projected["evidence_bundle"], original["evidence_bundle"])

    def test_records_keep_identity_and_evidence_fields(self):
        projected = project_verification_context(core_context())
        for item in projected["tool_execution_records"]:
            for field in ("record_id", "tool_id", "semantics", "status", "invocation_id"):
                self.assertIn(field, item)
            self.assertIn("evidence_refs", item)
        self.assertEqual(
            [item["record_id"] for item in projected["tool_execution_records"]],
            ["tool_1", "tool_2", "tool_3"],
        )

    def test_each_record_is_serialized_once(self):
        projected = project_verification_context(core_context())
        text = serialized(projected)
        records = projected["tool_execution_records"]
        # Lineage metadata that Core already validated is gone from the serialized request.
        self.assertEqual(text.count('"tool_spec_sha256"'), 0)
        # The record payload lives in exactly one place: the canonical record list. Neither the
        # inlined revision records nor the terminal-result summary repeat it.
        self.assertEqual(text.count('"response"'), len(records))
        self.assertEqual(
            [ref for ref in projected["plan_revisions"][0]["execution_records"]],
            ["tool_1", "tool_2", "tool_3"],
        )
        for result in projected["gateway_terminal_results"]:
            self.assertEqual(set(result), set(TERMINAL_RESULT_FIELDS))

    def test_projection_is_smaller_and_does_not_mutate_the_input(self):
        original = core_context()
        before = serialized(original)
        projected = project_verification_context(original)
        after = serialized(projected)
        self.assertLess(len(after), len(before))
        # The caller's context must come back unchanged: Core built it and may reuse it.
        self.assertEqual(serialized(original), before)
        self.assertIn("execution_records", original["plan_revisions"][0])

    def test_projection_tolerates_missing_sections(self):
        # A context without records must not raise; the keys stay present and empty.
        projected = project_verification_context({"criteria": ["c"]})
        self.assertEqual(projected["plan_revisions"], [])
        self.assertEqual(projected["tool_execution_records"], [])
        self.assertEqual(projected["gateway_terminal_results"], [])
        self.assertEqual(projected["criteria"], ["c"])

    def test_record_fields_matches_what_a_verifier_reads(self):
        self.assertIn("evidence_refs", RECORD_FIELDS)
        for dropped in ("tool_spec_sha256", "caller_id", "ownership"):
            self.assertNotIn(dropped, RECORD_FIELDS)


class InstallTest(unittest.TestCase):
    def test_install_rebinds_the_verifier_builder_once(self):
        from PhyAgentOS.agent import loop, session_verifier

        from paos_g1d.agent_integration import install
        from paos_g1d.verification import G1dVerificationRequestBuilder

        original_loop = loop.AgentLoop
        original_builder = session_verifier.VerificationRequestBuilder
        try:
            install()
            self.assertIs(
                session_verifier.VerificationRequestBuilder, G1dVerificationRequestBuilder
            )
            install()
            self.assertIs(
                session_verifier.VerificationRequestBuilder, G1dVerificationRequestBuilder
            )
        finally:
            loop.AgentLoop = original_loop
            session_verifier.VerificationRequestBuilder = original_builder

    def test_a_foreign_builder_extension_is_refused(self):
        from PhyAgentOS.agent import loop, session_verifier

        from paos_g1d.agent_integration import install

        class Foreign:  # pragma: no cover - only used to trip the guard
            pass

        original_loop = loop.AgentLoop
        original_builder = session_verifier.VerificationRequestBuilder
        try:
            session_verifier.VerificationRequestBuilder = Foreign
            with self.assertRaises(RuntimeError):
                install()
        finally:
            loop.AgentLoop = original_loop
            session_verifier.VerificationRequestBuilder = original_builder


class BuildRequestWiringTest(unittest.TestCase):
    """The override must actually run, not merely be correct in isolation."""

    def test_build_request_serializes_the_projected_context(self):
        from dataclasses import dataclass

        from paos_g1d.verification import G1dVerificationRequestBuilder

        @dataclass(frozen=True)
        class Validated:
            evidence: dict
            artifact_paths: tuple
            images: tuple
            structured: dict
            artifact_ids: frozenset

        validated = Validated({}, (), (), {}, frozenset())
        request = G1dVerificationRequestBuilder(".")._build_request(
            context=core_context(), validated=validated, valid_evidence_refs=frozenset()
        )
        body = request.content[0]["text"]
        # Lineage metadata never reaches the model, and the record payload appears once.
        self.assertNotIn("tool_spec_sha256", body)
        self.assertEqual(body.count('"response"'), 3)
        self.assertIn('"record_id": "tool_3"', body)
        self.assertIn(INVOCATION, json.dumps(core_context(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
