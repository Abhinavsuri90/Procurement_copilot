"""Architecture B end to end with a scripted fake LLM."""

from __future__ import annotations

from agent_helpers import HTTP, REPO, call, draft, submit, turn

from src.agents.llm import ScriptedBackend
from src.orchestrator import analyze
from src.schemas import Recommendation

PACK = {"need_summary": "Task tracker for campaign launches", "capabilities_needed": ["task tracking"],
        "existing_tool_candidates": [{"software_id": "SW003", "product_name": "TaskFlow", "fit": "full",
                                      "rationale": "company-wide project management", "call_id": "c2"}],
        "findings": [{"id": "F1", "claim": "TaskFlow SW003 has 180 seats", "source_tool": "search_existing_tools",
                      "call_id": "c2", "record_ids": ["SW003"], "value": "180"}],
        "open_questions": [], "concerns": []}


def analyst_turn():
    return turn(call("get_requester_profile", employee_id="E001"),
                call("search_existing_tools", query="task tracker", category="Project Management"),
                call("get_vendor_risk", vendor_name="TaskFlow"),
                call("check_budget", department="Marketing", amount_usd=8000))


def test_staged_pipeline_passes_pack_and_policy_to_reviewer():
    evidence = [{"id": "E1", "claim": "TaskFlow SW003 is approved company-wide", "source_tool": "search_existing_tools",
                 "call_id": "c2", "record_ids": ["SW003"], "value": ""}]
    backend = ScriptedBackend([analyst_turn(), turn(call("submit_evidence_pack", **PACK)),
                               submit(draft("use_existing_tool", evidence=evidence,
                                            next_step={"action": "redirect_to_existing_tool",
                                                       "owner_role": "Procurement", "detail": "Allocate seats"}))])
    result = analyze(REPO.get_request("REQ-1008"), "B", repo=REPO, http=HTTP, backend=backend)
    d = result.decision
    assert d.recommendation == Recommendation.USE_EXISTING_TOOL and d.meta.architecture == "B-staged-two-agent"
    assert d.meta.llm_calls == 3
    reviewer_req = backend.requests[2]
    assert [t["function"]["name"] for t in reviewer_req["tools"]] == ["submit_decision"]  # no data tools
    content = reviewer_req["messages"][1]["content"]
    assert "<analyst_evidence_pack>" in content and "policy_engine_result" in content and "SW003" in content
    assert result.raw_output["evidence_pack"]["need_summary"].startswith("Task tracker")


def test_staged_analyst_failure_is_failsafe():
    backend = ScriptedBackend([turn(content="no tools"), turn(content="still none")])
    d = analyze(REPO.get_request("REQ-1001"), "B", repo=REPO, http=HTTP, backend=backend).decision
    assert d.meta.deterministic_only and d.human_handoff.required


def test_staged_reviewer_cannot_approve_an_escalation():
    backend = ScriptedBackend([turn(call("get_vendor_risk", vendor_name="SignalWatch")),
                               turn(call("submit_evidence_pack", **{**PACK, "existing_tool_candidates": [],
                                                                     "findings": []})),
                               submit(draft("recommend_approve"))])
    d = analyze(REPO.get_request("REQ-1007"), "B", repo=REPO, http=HTTP, backend=backend).decision
    assert d.recommendation == Recommendation.ESCALATE_TO_HUMAN and d.overrides
    assert {"conflicting_vendor_evidence", "vendor_review_expired"} <= {f.code for f in d.risk_flags}
