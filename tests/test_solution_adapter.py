"""The starter adapter keeps working and maps every required field onto ProcurementDecision."""

from __future__ import annotations

from agent_helpers import HTTP, REPO
from src.contracts import ProcurementDecision
from src.orchestrator import analyze
from src.solution import handle_request, to_procurement_decision


def test_mapping_carries_all_required_fields():
    d = analyze(REPO.get_request("REQ-1006"), "R", repo=REPO, http=HTTP).decision
    p = to_procurement_decision(d)
    assert p.recommendation == "request_more_info" and p.human_review_required
    assert any("annual_cost_usd" in m for m in p.missing_information)
    assert "prompt_injection_detected" in p.risk_flags and "missing_information" in p.risk_flags
    assert p.required_approvals and p.evidence and p.next_step.startswith("Requester:")
    assert p.telemetry.tool_calls == d.meta.tool_calls


def test_handle_request_returns_a_valid_contract_without_a_key(monkeypatch):
    monkeypatch.setenv("VENDOR_SERVICE_FAULT", "down")
    monkeypatch.setenv("VENDOR_RETRIES", "0")
    monkeypatch.setenv("CASSETTE_DIR", "/nonexistent")
    for arch in ("single", "staged"):
        out = handle_request("REQ-1001", architecture=arch)
        assert isinstance(out, ProcurementDecision)
        ProcurementDecision.model_validate(out.model_dump())
        assert "vendor_risk_unavailable" in out.risk_flags and "ai_unavailable" in out.risk_flags
