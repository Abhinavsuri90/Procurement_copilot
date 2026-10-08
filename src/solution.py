"""Assessment adapter: `handle_request(request_id, architecture)` -> starter `ProcurementDecision`.

The public/hidden evaluation harness calls this. Internally it runs the copilot (A = "single", B = "staged") and
maps the richer `Decision` onto the starter contract without losing any of the six required fields.
"""

from __future__ import annotations

from src.contracts import Architecture, EvidenceItem, ProcurementDecision, RunTelemetry
from src.data_access import default_repository
from src.orchestrator import analyze
from src.schemas import Decision


def to_procurement_decision(decision: Decision) -> ProcurementDecision:
    step = decision.next_step
    return ProcurementDecision(
        request_id=decision.request_id,
        recommendation=decision.recommendation.value,
        evidence=[EvidenceItem(source=e.source_tool, finding=e.claim + (f" [{e.value}]" if e.value else ""),
                               reference=", ".join([*e.record_ids, e.call_id])) for e in decision.evidence],
        required_approvals=[a.role for a in decision.approvals_required],
        missing_information=[f"{m.field}: {m.question_for_requester}" for m in decision.missing_information],
        risk_flags=[f.code for f in decision.risk_flags],
        next_step=f"{step.owner_role}: {step.action} - {step.detail}",
        # Policy section 11: a human approves every purchase, so review is always required. Whether the request
        # also needs escalation beyond routine sign-off is in Decision.human_handoff.
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=decision.meta.llm_calls, tool_calls=decision.meta.tool_calls,
                               tool_names=decision.meta.tool_names),
    )


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter.

    Keep this function callable by the public/hidden evaluation harness.
    Your internal implementation may use any framework, modules, agents, tools,
    deterministic checks, or orchestration strategy.
    """
    repo = default_repository()
    result = analyze(repo.get_request(request_id), architecture, repo=repo, case_key=(request_id, 1))
    return to_procurement_decision(result.decision)
