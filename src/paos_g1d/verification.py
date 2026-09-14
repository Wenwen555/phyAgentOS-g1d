"""Clarify Core's evidence-reference contract and stop sending the same records three times.

Two project-side adjustments to Core's Forge verification request, both inside the existing
subclass so Core's validation is untouched:

1. `_build_request` projects the context before it is serialized. Core's builder emits the
   task's execution records three times over (inside `plan_revisions`, as
   `tool_execution_records`, and again as `gateway_terminal_results`). On a real nine-record
   lift task that was 39.6k of a 43k-character context, with 25.9k characters of pure
   duplication. The copies are supersets of one another, so keeping the complete one and
   reducing the other two to identifiers removes no information.
2. `build_agent_task` appends the verbatim list of allowed evidence references, because
   Core's validator rejects invented labels and the model otherwise guesses them.

The projection must never make an evidence reference unresolvable. `valid_evidence_refs` is
the union of every record's own `evidence_refs` field and the evidence bundle's
`artifact_id`s, so the projection keeps each record's `evidence_refs` and identity fields
intact and leaves `evidence_bundle` alone.
"""

import json
from dataclasses import replace

from PhyAgentOS.verification.request_builder import VerificationRequestBuilder

#: Fields of a ToolExecutionRecord dump a verifier can act on. Everything else is lineage
#: metadata (`version`, `skill_binding_id`, `tool_spec_sha256`, `caller_id`, `ownership`,
#: timestamps) that Core already validated before the request was built, and that the
#: verifier only had to read past.
RECORD_FIELDS = (
    "record_id",
    "revision_id",
    "tool_id",
    "semantics",
    "status",
    "invocation_id",
    "attempt_id",
    "arguments",
    "response",
    "error",
    "evidence_refs",
)

#: Keys of a gateway terminal result that stay meaningful once the record payload is no
#: longer duplicated here; the full result is still in `tool_execution_records`.
TERMINAL_RESULT_FIELDS = ("record_id", "tool_id", "status")


def project_verification_context(context: dict) -> dict:
    """Return a copy of the verification context with the duplicated record payloads removed.

    `plan_revisions` keeps its revision metadata and verdicts but lists member record ids
    instead of inlining the records; `gateway_terminal_results` keeps the identifiers of the
    terminal executions instead of repeating their responses; `tool_execution_records` stays
    complete and becomes the single place the full payload lives.
    """
    projected = dict(context)
    projected["plan_revisions"] = [
        {
            **revision,
            "execution_records": [
                record.get("record_id") for record in revision.get("execution_records") or []
            ],
        }
        for revision in context.get("plan_revisions") or []
    ]
    projected["tool_execution_records"] = [
        {key: record[key] for key in RECORD_FIELDS if key in record}
        for record in context.get("tool_execution_records") or []
    ]
    projected["gateway_terminal_results"] = [
        {key: result.get(key) for key in TERMINAL_RESULT_FIELDS}
        for result in context.get("gateway_terminal_results") or []
    ]
    return projected


class G1dVerificationRequestBuilder(VerificationRequestBuilder):
    def _build_request(self, *, context, validated, valid_evidence_refs):
        return super()._build_request(
            context=project_verification_context(context),
            validated=validated,
            valid_evidence_refs=valid_evidence_refs,
        )

    def build_agent_task(self, task, *, events, lessons):
        request = super().build_agent_task(task, events=events, lessons=lessons)
        reminder = {
            "type": "text",
            "text": (
                "Evidence reference serialization requirement: every evidence_refs entry, both "
                "overall and per criterion, MUST be copied verbatim from the following allowed "
                "values. Do not invent concise labels or add prefixes such as tool:. A record_id "
                "is not automatically a valid evidence reference. This is only a formatting "
                "constraint; judge every criterion independently from the supplied facts and "
                "evidence, and report uncertainty or failure when warranted.\n"
                + json.dumps(sorted(request.valid_evidence_refs))
            ),
        }
        return replace(request, content=[*request.content, reminder])
