"""Architecture A end to end with a scripted fake LLM: the loop, every guardrail, fail-safes and record/replay."""

from __future__ import annotations

import dataclasses

from agent_helpers import HTTP, REPO, call, draft, gather, submit, turn

from src.agents.llm import LLMError, ScriptedBackend
from src.config import get_settings
from src.orchestrator import analyze
from src.schemas import Recommendation

GOOD_EVIDENCE = [
    {"id": "E1", "claim": "Finance has 29000 available and the request of 800 is within budget",
     "source_tool": "check_budget", "call_id": "c2", "record_ids": ["BUDGET:Finance"], "value": "29000"},
    {"id": "E2", "claim": "SignFlow security status approved, reviewed 2026-06-20", "source_tool": "get_vendor_risk",
     "call_id": "c4", "record_ids": ["V010", "RISK:SignFlow"], "value": "approved"},
]


def run(request_id: str, script, arch: str = "A", **kw):
    backend = ScriptedBackend(script)
    result = analyze(REPO.get_request(request_id), arch, repo=REPO, http=HTTP, backend=backend, **kw)
    return result, backend


def test_happy_path_keeps_agent_decision_and_grounded_evidence():
    result, backend = run("REQ-1001", [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"),
                                       submit(draft(approvals=[("Manager", "POL-4.T1")], evidence=GOOD_EVIDENCE))])
    d = result.decision
    assert d.recommendation == Recommendation.RECOMMEND_APPROVE and not d.overrides
    assert [a.role for a in d.approvals_required] == ["Manager"]
    assert {e.id for e in d.evidence if e.origin == "agent"} == {"E1", "E2"}
    assert d.meta.llm_calls == 2 and d.meta.tool_calls >= 5
    assert d.meta.guardrails["raw_policy_adherent"] is True
    # tool results reach the model as untrusted data with our sequential call ids
    tool_msgs = [m for m in backend.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "c1" and tool_msgs[0]["content"].startswith("UNTRUSTED TOOL OUTPUT")


def test_agent_cannot_drop_policy_approvals_or_approve_an_escalation():
    result, _ = run("REQ-1005", [gather("REQ-1005", "E003", "Sales", 22000, "GrowthForge"),
                                 submit(draft(approvals=[("Manager", "POL-4.T1")]))])
    d = result.decision
    assert d.recommendation == Recommendation.ESCALATE_TO_HUMAN
    assert {"Department Head", "Finance", "Procurement", "Security", "Privacy", "Legal"} <= {a.role for a in d.approvals_required}
    assert d.overrides and d.overrides[0].agent_value == "recommend_approve"
    assert d.human_handoff.required
    assert {"budget_insufficient", "vendor_not_approved"} <= {f.code for f in d.risk_flags}
    assert d.meta.guardrails["raw_policy_adherent"] is False


def test_injected_request_cannot_get_approved():
    result, _ = run("REQ-1006", [gather("REQ-1006", "E001", "Marketing", None, "NeuralDesk"),
                                 submit(draft(approvals=[("CFO", "POL-4.T4")]))])
    d = result.decision
    assert d.recommendation == Recommendation.REQUEST_MORE_INFO
    assert "prompt_injection_detected" in {f.code for f in d.risk_flags} and d.human_handoff.required
    assert {"annual_cost_usd", "user_count", "data_access_level"} <= {m.field for m in d.missing_information}


def test_uncited_agent_approval_is_dropped_and_cited_one_kept():
    result, _ = run("REQ-1001", [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"),
                                 submit(draft(approvals=[("Manager", "POL-4.T1"), ("Legal", "made-up"),
                                                         ("Security", "POL-5.DATA")]))])
    d = result.decision
    roles = {a.role: a.source for a in d.approvals_required}
    assert roles == {"Manager": "policy_engine", "Security": "agent"}
    assert d.meta.guardrails["uncited_approvals_dropped"] == [{"role": "Legal", "rule_id": "made-up"}]


def test_ungrounded_evidence_is_removed():
    bad = [{"id": "X1", "claim": "Budget available is 99999", "source_tool": "check_budget", "call_id": "c2",
            "record_ids": ["BUDGET:Finance"], "value": "99999"},
           {"id": "X2", "claim": "Vendor approved", "source_tool": "get_vendor_risk", "call_id": "c99",
            "record_ids": ["V010"], "value": ""},
           {"id": "X3", "claim": "Vendor approved", "source_tool": "check_budget", "call_id": "c4",
            "record_ids": ["V010"], "value": ""},
           {"id": "X4", "claim": "Vendor record", "source_tool": "get_vendor_risk", "call_id": "c4",
            "record_ids": ["V777"], "value": ""}]
    result, _ = run("REQ-1001", [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"),
                                 submit(draft(approvals=[("Manager", "POL-4.T1")], evidence=bad + GOOD_EVIDENCE))])
    g = result.decision.meta.guardrails
    assert g["evidence_total"] == 6 and g["evidence_grounded"] == 2
    assert {r["id"] for r in g["ungrounded_removed"]} == {"X1", "X2", "X3", "X4"}
    assert all(e.call_id in {t["payload"]["call_id"] for t in result.trace if t["kind"] == "tool"}
               for e in result.decision.evidence)


def test_reject_without_policy_block_becomes_escalation():
    result, _ = run("REQ-1008", [gather("REQ-1008", "E001", "Marketing", 8000, "TaskFlow"),
                                 submit(draft("recommend_reject"))])
    assert result.decision.recommendation == Recommendation.ESCALATE_TO_HUMAN


def test_use_existing_tool_requires_grounded_catalog_evidence():
    no_evidence, _ = run("REQ-1008", [gather("REQ-1008", "E001", "Marketing", 8000, "TaskFlow"),
                                      submit(draft("use_existing_tool"))])
    assert no_evidence.decision.overrides
    catalog = [{"id": "E1", "claim": "TaskFlow SW003 is approved company-wide with 180 seats",
                "source_tool": "search_existing_tools", "call_id": "c3", "record_ids": ["SW003"], "value": "180"}]
    script = [turn(call("get_requester_profile", employee_id="E001"),
                   call("check_budget", department="Marketing", amount_usd=8000),
                   call("search_existing_tools", query="TaskFlow task tracker", category="Project Management"),
                   call("get_vendor_risk", vendor_name="TaskFlow"), call("evaluate_policy", request_id="REQ-1008")),
              submit(draft("use_existing_tool", approvals=[("Department Head", "POL-4.T2")], evidence=catalog,
                           next_step={"action": "redirect_to_existing_tool", "owner_role": "Procurement",
                                      "detail": "Allocate TaskFlow seats"}))]
    with_evidence, _ = run("REQ-1008", script)
    d = with_evidence.decision
    assert d.recommendation == Recommendation.USE_EXISTING_TOOL and not d.overrides
    assert d.next_step.owner_role == "Procurement" and not d.human_handoff.required


def test_invalid_output_is_repaired_once():
    broken = draft()
    del broken["next_step"]
    result, backend = run("REQ-1001", [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"), submit(broken),
                                       submit(draft(approvals=[("Manager", "POL-4.T1")]))])
    assert not result.decision.meta.deterministic_only and result.raw_output["repaired"]
    repair_msg = backend.requests[2]["messages"][-1]
    assert "schema validation failed" in repair_msg["content"]


def test_invalid_output_twice_is_failsafe():
    broken = draft(recommendation="buy_it_now")
    result, _ = run("REQ-1001", [submit(broken), submit(broken)])
    d = result.decision
    assert d.meta.deterministic_only and d.recommendation == Recommendation.ESCALATE_TO_HUMAN
    assert "agent_output_invalid" in {f.code for f in d.risk_flags} and d.human_handoff.required
    for field in ("recommendation", "evidence", "approvals_required", "missing_information", "risk_flags", "next_step"):
        assert getattr(d, field) is not None


def test_llm_failure_gives_deterministic_decision_handed_to_human():
    result, _ = run("REQ-1001", [LLMError("HTTP 503")])
    d = result.decision
    assert d.meta.deterministic_only and "ai_unavailable" in {f.code for f in d.risk_flags}
    assert d.recommendation == Recommendation.ESCALATE_TO_HUMAN and d.human_handoff.required
    assert [a.role for a in d.approvals_required] == ["Manager"]


def test_model_that_never_calls_submit_is_nudged_then_failsafe():
    result, backend = run("REQ-1001", [turn(content="I think approve."), turn(content="Still thinking.")])
    assert result.decision.meta.deterministic_only and len(backend.requests) == 2
    assert "did not call a tool" in backend.requests[1]["messages"][-1]["content"]


def test_turn_cap_is_enforced():
    script = [turn(call("get_requester_profile", employee_id="E004")) for _ in range(12)]
    result, backend = run("REQ-1001", script)
    assert len(backend.requests) == 10 and result.decision.meta.deterministic_only


def test_vendor_outage_never_yields_approval():
    result, _ = run("REQ-1001", [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"),
                                 submit(draft(approvals=[("Manager", "POL-4.T1")]))], fault="down")
    d = result.decision
    assert d.recommendation == Recommendation.ESCALATE_TO_HUMAN
    assert "vendor_risk_unavailable" in {f.code for f in d.risk_flags}


def test_record_then_replay_reproduces_the_decision(tmp_path):
    record = dataclasses.replace(get_settings(), llm_mode="record", cassette_dir=tmp_path)
    script = [gather("REQ-1001", "E004", "Finance", 800, "SignFlow"),
              submit(draft(approvals=[("Manager", "POL-4.T1")], evidence=GOOD_EVIDENCE))]
    recorded = analyze(REPO.get_request("REQ-1001"), "A", repo=REPO, http=HTTP, settings=record,
                       backend=ScriptedBackend(script), case_key=("REQ-1001", 1)).decision
    cassette = tmp_path / "A" / "REQ-1001" / "run1.json"
    assert cassette.is_file() and "api_key" not in cassette.read_text().lower()

    replay = dataclasses.replace(get_settings(), llm_mode="replay", cassette_dir=tmp_path, llm_api_key="")
    replayed = analyze(REPO.get_request("REQ-1001"), "A", repo=REPO, http=HTTP, settings=replay,
                       case_key=("REQ-1001", 1)).decision
    assert replayed.meta.replayed and replayed.meta.latency_ms == recorded.meta.latency_ms
    strip = {"meta"}
    assert replayed.model_dump(exclude=strip) == recorded.model_dump(exclude=strip)
    assert (replayed.meta.llm_calls, replayed.meta.tool_calls) == (recorded.meta.llm_calls, recorded.meta.tool_calls)

    # a different request (here: a fault that changes tool output) no longer matches the recording
    drifted = analyze(REPO.get_request("REQ-1001"), "A", repo=REPO, http=HTTP, settings=replay, fault="down",
                      case_key=("REQ-1001", 1)).decision
    assert drifted.meta.deterministic_only and any("mismatch" in w for w in drifted.meta.warnings)


def test_next_step_owner_must_fit_the_recommendation():
    catalog = [{"id": "E1", "claim": "TaskFlow SW003 is approved company-wide", "source_tool": "search_existing_tools",
                "call_id": "c3", "record_ids": ["SW003"], "value": ""}]
    result, _ = run("REQ-1008", [gather("REQ-1008", "E001", "Marketing", 8000, "TaskFlow"),
                                 submit(draft("use_existing_tool", evidence=catalog,
                                              next_step={"action": "route_for_approval", "owner_role": "Department Head",
                                                         "detail": "approve"}))])
    d = result.decision
    assert d.recommendation == Recommendation.USE_EXISTING_TOOL
    assert d.next_step.owner_role == "Procurement" and d.meta.guardrails["next_step_replaced"] is True
