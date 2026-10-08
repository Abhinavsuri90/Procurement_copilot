"""Tools: envelopes, budget math, vendor expiry/conflict logic, failure paths, catalog search, history."""

from __future__ import annotations

from datetime import date

import httpx

import src.tools  # noqa: F401  (registers tools)
from src.data_access import VendorRecord
from src.tools.base import execute
from src.tools.business import assess_vendor
from src.vendor_client import VendorClient, VendorLookup


def test_envelope_shape_and_sequential_call_ids(make_ctx):
    ctx = make_ctx()
    a = execute(ctx, "get_requester_profile", {"employee_id": "E004"})
    b = execute(ctx, "get_requester_profile", {"employee_id": "NOPE"})
    assert (a.call_id, b.call_id) == ("c1", "c2")
    assert a.ok and a.data["employee"]["employee_id"] == "E004"
    assert a.data["manager"]["employee_id"] == "E006" and a.data["department_head"]["employee_id"] == "E006"
    assert not b.ok and b.error.code == "NOT_FOUND" and b.data is None
    assert set(a.for_llm()) == {"call_id", "tool", "ok", "data", "error", "source"}  # no latency -> stable hashes


def test_invalid_arguments_and_unknown_tool_become_error_envelopes(make_ctx):
    ctx = make_ctx()
    assert execute(ctx, "check_budget", {"amount_usd": -5}).error.code == "INVALID_ARGUMENTS"
    assert execute(ctx, "delete_budget", {}).error.code == "UNKNOWN_TOOL"
    assert ctx.trace.tool_call_count == 2


def test_budget_math_boundary(make_ctx):
    ctx = make_ctx()
    at = execute(ctx, "check_budget", {"department": "Customer Success", "amount_usd": 7000}).data
    over = execute(ctx, "check_budget", {"department": "customer success", "amount_usd": 7000.01}).data
    assert at["within_budget"] is True and at["remaining_after_usd"] == 0
    assert over["within_budget"] is False and over["record_id"] == "BUDGET:Customer Success"
    assert at["utilisation_after_pct"] == 100.0


def test_budget_uses_smaller_figure_when_snapshot_is_inconsistent(make_ctx):
    overlay = {"department_budgets": [{"department": "Finance", "annual_software_budget_usd": "90000",
                                       "committed_usd": "80000", "available_usd": "29000"}]}
    data = execute(make_ctx(overlay=overlay), "check_budget", {"department": "Finance", "amount_usd": 15000}).data
    assert data["arithmetic_consistent"] is False and data["effective_available_usd"] == 10000
    assert data["within_budget"] is False


def test_budget_without_amount_reports_unknown(make_ctx):
    data = execute(make_ctx(), "check_budget", {"department": "Finance"}).data
    assert data["within_budget"] is None


def _lookup(**record):
    return VendorLookup("ok", record={"vendor_name": "V", **record})


REG = VendorRecord(vendor_id="V9", vendor_name="V", procurement_status="Approved", security_status="Approved",
                   security_review_date="2025-10-01", legal_terms_status="Approved")


def test_assessment_expiry_boundary_364_vs_365_days():
    as_of = date(2026, 9, 30)
    current = assess_vendor("V", REG.model_copy(update={"security_review_date": "2025-10-01"}),
                            _lookup(security_review_status="approved", last_review_date="2025-10-01"), as_of, 365)
    expired = assess_vendor("V", REG.model_copy(update={"security_review_date": "2025-09-30"}),
                            _lookup(security_review_status="approved", last_review_date="2025-09-30"), as_of, 365)
    assert current["assessment"]["days_since_review"] == 364 and current["effective_status"] == "approved"
    assert expired["assessment"]["days_since_review"] == 365 and expired["effective_status"] == "expired"


def test_registry_service_conflict_takes_most_conservative_status():
    out = assess_vendor("V", REG.model_copy(update={"security_review_date": "2025-07-01"}),
                        _lookup(security_review_status="expired", last_review_date="2025-07-01"), date(2026, 9, 30), 365)
    assert [c["field"] for c in out["conflicts"]] == ["security_status"]
    assert out["effective_status"] == "expired" and out["expired_items"]


def test_conflicting_review_dates_use_the_older_date():
    out = assess_vendor("V", REG.model_copy(update={"security_review_date": "2026-06-01"}),
                        _lookup(security_review_status="approved", last_review_date="2025-01-01"), date(2026, 9, 30), 365)
    assert out["assessment"]["review_date_used"] == "2025-01-01" and out["effective_status"] == "expired"
    assert out["conflicts"][0]["field"] == "review_date"


def test_registry_approved_but_service_has_no_record_is_a_conflict():
    out = assess_vendor("V", REG, VendorLookup("not_found", error="no record"), date(2026, 9, 30), 365)
    assert out["conflicts"][0]["field"] == "assessment_record"


def test_vendor_tool_on_starter_conflict_case(make_ctx):
    r = execute(make_ctx(), "get_vendor_risk", {"vendor_name": "SignalWatch"})
    assert r.ok and r.data["effective_status"] == "expired" and r.data["conflicts"]
    assert r.data["risk_service"]["record_id"] == "RISK:SignalWatch"


def test_vendor_service_503_returns_unavailable_envelope_with_registry_data(make_ctx):
    r = execute(make_ctx(), "get_vendor_risk", {"vendor_name": "NimbusAI"})
    assert not r.ok and r.error.code == "VENDOR_SERVICE_UNAVAILABLE" and r.error.retryable
    assert r.data["registry_record"]["vendor_id"] == "V013" and r.data["effective_status"] == "unverified"
    assert r.data["risk_service"]["attempts"] == 3  # 1 try + 2 retries


def test_fault_injection_down_and_slow_degrade_safely(make_ctx):
    for fault in ("down", "slow"):
        r = execute(make_ctx(fault=fault), "get_vendor_risk", {"vendor_name": "SignFlow"})
        assert not r.ok and r.error.code == "VENDOR_SERVICE_UNAVAILABLE"


def test_flaky_fault_recovers_on_retry(make_ctx):
    r = execute(make_ctx(fault="flaky"), "get_vendor_risk", {"vendor_name": "SignFlow"})
    assert r.ok and r.data["risk_service"]["attempts"] == 2


def test_vendor_client_retries_5xx_and_not_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "Missing" in request.url.path:
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(502, json={"detail": "bad gateway"})

    client = VendorClient("http://x", retries=2, http=httpx.Client(base_url="http://x",
                                                                   transport=httpx.MockTransport(handler)),
                          sleep=lambda s: None)
    assert client.get_vendor_risk("Flaky").status == "unavailable" and calls["n"] == 3
    calls["n"] = 0
    assert client.get_vendor_risk("Missing").status == "not_found" and calls["n"] == 1


def test_catalog_search_finds_same_product_and_capability(make_ctx):
    ctx = make_ctx()
    hits = execute(ctx, "search_existing_tools", {"query": "TaskFlow Pro", "category": "Project Management",
                                                  "vendor_name": "TaskFlow"}).data["matches"]
    assert hits[0]["software_id"] == "SW003"
    assert {"same_vendor", "same_product", "same_category"} <= set(hits[0]["match_reasons"])
    capability = execute(ctx, "search_existing_tools", {"query": "KPI dashboards and reporting"}).data["matches"]
    assert capability[0]["software_id"] == "SW006"


def test_purchase_history_filters_and_flags_duplicates(make_ctx):
    data = execute(make_ctx(), "get_purchase_history", {"vendor_name": "TaskFlow", "product_name": "TaskFlow"}).data
    assert [p["purchase_id"] for p in data["purchases"]] == ["PO-2501"]
    assert data["purchases"][0]["company_wide"] is True and data["possible_duplicates"]
