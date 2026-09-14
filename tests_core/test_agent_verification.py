"""Formatting guidance must preserve native evidence and strict verdict validation."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PhyAgentOS.agent.session_verifier import (  # noqa: E402
    ForgeTaskVerifier,
    VerificationVerdictError,
)
from PhyAgentOS.verification.contracts import (  # noqa: E402
    EvidenceBundle,
    EvidenceQuality,
    VerificationVerdict,
)
from PhyAgentOS.verification.request_builder import VerificationRequest  # noqa: E402

from paos_g1d.verification import G1dVerificationRequestBuilder  # noqa: E402


class VerificationContractTest(unittest.TestCase):
    def test_request_keeps_native_evidence_and_only_adds_reference_guidance(self):
        content = [{"type": "text", "text": "original facts"}]
        evidence = EvidenceBundle(bundle_id="b", session_id="t", command_id="agent_task",
                                  quality=EvidenceQuality(complete=True))
        request = VerificationRequest(content, (), frozenset({"invocation:abc"}), evidence)
        with patch("PhyAgentOS.verification.request_builder.VerificationRequestBuilder.build_agent_task",
                   return_value=request):
            result = G1dVerificationRequestBuilder(".").build_agent_task(None, events=[], lessons="[]")
        self.assertEqual(content, [{"type": "text", "text": "original facts"}])
        self.assertEqual(result.content[:-1], request.content)
        self.assertIs(result.evidence, request.evidence)
        self.assertIs(result.valid_evidence_refs, request.valid_evidence_refs)
        self.assertIn('"invocation:abc"', result.content[-1]["text"])

    def test_native_validator_still_rejects_invented_references(self):
        verdict = VerificationVerdict(verdict="success", criteria=[
            {"criterion": "stopped", "status": "satisfied", "evidence_refs": ["tool:invented"]}
        ], evidence_refs=[], reason="observed", lesson="check stop")
        with self.assertRaises(VerificationVerdictError):
            ForgeTaskVerifier._validate_generic_verdict(
                expected_criteria=["stopped"], valid_evidence_refs={"invocation:abc"}, verdict=verdict
            )


if __name__ == "__main__":
    unittest.main()
