"""A verifier that fails must not turn a successful physical Action into a failed task.

The semantic verifier is a model call with a bounded token budget. On this deployment
it has returned truncated JSON and empty content, and Core records a verifier error as
a task failure whenever the verification mode gates the status. These tests pin the
mitigation this project relies on: `audit` decides the task from the execution facts
when the verifier itself fails, while `enforce` keeps the verdict authoritative.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PhyAgentOS.forge.task import (  # noqa: E402
    AgentTaskCoordinator,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentTaskStore,
    PlanRevision,
    ToolExecutionRecord,
)
from PhyAgentOS.verification.contracts import TaskVerificationContract  # noqa: E402


def record(mode, *, action_status, revision_id="revision-1"):
    return AgentTaskRecord(
        task_id="task-verifier-failure",
        task_description="Move the column to the commanded height",
        verification=TaskVerificationContract(
            mode=mode, goal="Reach the commanded height", success_criteria=["reached the goal"]
        ),
        revisions=[
            PlanRevision(
                revision_id=revision_id,
                number=1,
                reason="initial plan",
                execution_records=[
                    ToolExecutionRecord(
                        record_id="record-1",
                        revision_id=revision_id,
                        tool_id="g1d.set_height",
                        semantics="action",
                        caller_id="agent",
                        status=action_status,
                        invocation_id="invocation-1",
                    )
                ],
            )
        ],
        active_revision_id=revision_id,
    )


class VerifierFailureTest(unittest.TestCase):
    def coordinator(self, tmp):
        return AgentTaskCoordinator(
            workspace=tmp, config=None, client=None, store=AgentTaskStore(tmp)
        )

    def run_case(self, mode, action_status):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = self.coordinator(tmp)
            coordinator.store.create(record(mode, action_status=action_status))
            result = coordinator._verification_error(
                "task-verifier-failure", "verifier model returned no content"
            )
            self.assertTrue(result.evidence_errors)
            self.assertIn("verification failed", result.evidence_errors[0])
            return result.status

    def test_enforce_mode_still_fails_when_the_verifier_errors(self):
        # Documented Core behavior: enforce is authoritative, so a verifier outage is
        # reported as a failed task. This is why a numeric workflow must not use it.
        self.assertIs(
            self.run_case("enforce", "succeeded"),
            AgentTaskStatus.FAILED,
        )

    def test_audit_mode_survives_a_verifier_error_when_the_action_succeeded(self):
        # The mitigation: the verdict is still recorded, but a verifier failure cannot
        # override execution facts that say the physical Action succeeded.
        self.assertIs(
            self.run_case("audit", "succeeded"),
            AgentTaskStatus.SUCCEEDED,
        )

    def test_audit_mode_is_not_a_blanket_pass(self):
        # Audit must not launder a genuinely failed Action into success.
        self.assertIs(
            self.run_case("audit", "failed"),
            AgentTaskStatus.FAILED,
        )


if __name__ == "__main__":
    unittest.main()
