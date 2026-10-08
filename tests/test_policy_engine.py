"""Policy engine: every threshold at boundary -0.01 / = / +0.01, every rule ID, and the escalation floor."""

from __future__ import annotations

import pytest

from src.policy.engine import evaluate
from src.policy.facts import Facts
from src.policy.rules import load_rules
from src.schemas import Recommendation

RULES = load_rules()


def facts(**kw) -> Facts:
    """A clean, approvable baseline: approved vendor, current review, in budget, non-sensitive data."""
    base = dict(request_id="T", requester_id="E004", product_name="Thing", vendor_name="GoodCo", category="Misc",
                amount_usd=500.0, user_count=5, purpose="Valid reason", data_access_level="internal_documents",
                data_access_raw="internal_documents", integrations=[], requester_found=True, department="Finance",
                budget_status="within", budget_effective_available=29000.0, vendor_lookup="ok",
                vendor_registry_found=True, vendor_is_new=False, vendor_procurement_status="Approved",
                vendor_legal_terms="Approved", vendor_effective_status="approved", vendor_risk_level="low",
                vendor_processes_personal_data=False, vendor_stores_outside_region=False)
    base.update(kw)
    return Facts(**base)


def roles(result) -> set[str]:
    return set(result.approval_roles)


@pytest.mark.parametrize("amount,expected,tier", [
    (999.99, {"Manager"}, "POL-4.T1"),
    (1000.00, {"Manager"}, "POL-4.T1"),
    (1000.01, {"Department Head", "Procurement"}, "POL-4.T2"),
    (9999.99, {"Department Head", "Procurement"}, "POL-4.T2"),
    (10000.00, {"Department Head", "Procurement"}, "POL-4.T2"),
    (10000.01, {"Department Head", "Finance", "Procurement"}, "POL-4.T3"),
    (24999.99, {"Department Head", "Finance", "Procurement"}, "POL-4.T3"),
    (25000.00, {"Department Head", "Finance", "Procurement"}, "POL-4.T3"),
    (25000.01, {"Department Head", "Finance", "CFO", "Procurement"}, "POL-4.T4"),
])
def test_approval_thresholds_at_boundaries(amount, expected, tier):
    r = evaluate(facts(amount_usd=amount, budget_effective_available=10**6))
    assert roles(r) == expected
    assert r.tier_rule_id == tier


def test_cfo_tier_forces_handoff_but_not_escalation():
    r = evaluate(facts(amount_usd=25000.01, budget_effective_available=10**6))
    assert r.handoff_required and "senior_approval_required" in r.flag_codes
    assert r.deterministic_recommendation == Recommendation.RECOMMEND_APPROVE


def test_clean_low_value_request_is_approvable_without_handoff():
    r = evaluate(facts())
    assert r.deterministic_recommendation == Recommendation.RECOMMEND_APPROVE
    assert not r.handoff_required and r.next_step["owner_role"] == "Manager"


@pytest.mark.parametrize("status,flagged", [("within", False), ("exceeded", True)])
def test_budget_exceeded_adds_finance_and_escalates(status, flagged):
    r = evaluate(facts(budget_status=status))
    assert ("budget_insufficient" in r.flag_codes) is flagged
    assert ("Finance" in roles(r)) is flagged
    assert (r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN) is flagged


def test_unverifiable_budget_is_flagged():
    r = evaluate(facts(budget_status="no_record", department="Go To Market"))
    assert "budget_unverified" in r.flag_codes and "Finance" in roles(r)


@pytest.mark.parametrize("data_class", ["source_code", "customer_pii", "employee_pii", "confidential_documents",
                                        "credentials", "production_telemetry"])
def test_sensitive_data_requires_security_review(data_class):
    r = evaluate(facts(data_access_level=data_class, data_access_raw=data_class))
    assert "Security" in roles(r) and "security_review_required" in r.flag_codes
    assert r.handoff_required


@pytest.mark.parametrize("data_class,privacy", [("customer_pii", True), ("employee_pii", True), ("source_code", False)])
def test_privacy_review_for_pii(data_class, privacy):
    r = evaluate(facts(data_access_level=data_class, data_access_raw=data_class))
    assert ("Privacy" in roles(r)) is privacy


def test_unknown_data_class_is_treated_as_sensitive():
    r = evaluate(facts(data_access_level="mystery_data", data_access_raw="mystery_data"))
    assert "Security" in roles(r) and "unknown_data_class" in r.flag_codes and r.handoff_required


def test_production_integration_requires_security():
    r = evaluate(facts(integrations=["Production cloud account"]))
    assert "Security" in roles(r)
    assert any(rv.rule_id == "POL-5.INTEGRATION" for rv in r.reviews)


def test_integration_keyword_matching_uses_word_boundaries():
    r = evaluate(facts(integrations=["Product analytics export"]))
    assert "Security" not in roles(r)


@pytest.mark.parametrize("amount,legal", [(9999.99, False), (10000.00, True), (10000.01, True)])
def test_legal_for_new_vendor_at_10k_inclusive(amount, legal):
    r = evaluate(facts(amount_usd=amount, vendor_is_new=True, vendor_legal_terms="Approved",
                       budget_effective_available=10**6))
    assert any(rv.rule_id == "POL-7.NEW_VENDOR" for rv in r.reviews) is legal


def test_unapproved_legal_terms_require_legal():
    r = evaluate(facts(vendor_legal_terms="Draft"))
    assert "Legal" in roles(r) and "legal_review_required" in r.flag_codes


def test_cross_region_personal_data_requires_privacy_and_legal_and_escalates():
    r = evaluate(facts(data_access_level="customer_pii", data_access_raw="customer_pii",
                       vendor_stores_outside_region=True))
    assert {"Privacy", "Legal", "Security"} <= roles(r)
    assert r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN


@pytest.mark.parametrize("status,code", [("expired", "vendor_review_expired"), ("not_completed", "vendor_assessment_missing"),
                                         ("unknown", "vendor_assessment_missing")])
def test_uncleared_vendor_status_escalates(status, code):
    r = evaluate(facts(vendor_effective_status=status, vendor_expired_items=["x"] if status == "expired" else []))
    assert code in r.flag_codes and "Security" in roles(r)
    assert r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN


def test_vendor_conflict_is_surfaced_not_resolved():
    r = evaluate(facts(vendor_conflicts=[{"field": "security_status", "registry": "Approved", "service": "expired"}]))
    assert "conflicting_vendor_evidence" in r.flag_codes
    assert r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN


def test_vendor_service_unavailable_never_infers_favourable_status():
    r = evaluate(facts(vendor_lookup="unavailable", vendor_effective_status="unverified",
                       vendor_stores_outside_region=None, data_access_level="confidential_documents",
                       data_access_raw="confidential_documents"))
    assert "vendor_risk_unavailable" in r.flag_codes
    assert {"Security", "Privacy"} <= roles(r)  # region unverified -> privacy (assumption A6)
    assert r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN


def test_rejected_vendor_is_a_block():
    r = evaluate(facts(vendor_effective_status="rejected"))
    assert r.blocks and r.deterministic_recommendation == Recommendation.RECOMMEND_REJECT


def test_missing_information_requests_more_info_with_questions():
    r = evaluate(facts(amount_usd=None, user_count=None, missing=["annual_cost_usd", "user_count"],
                       budget_status="unknown_amount"))
    assert r.deterministic_recommendation == Recommendation.REQUEST_MORE_INFO
    assert {m.field for m in r.missing_fields} == {"annual_cost_usd", "user_count"}
    assert all(m.question.endswith("?") for m in r.missing_fields)
    assert r.tier_rule_id is None and r.next_step["owner_role"] == "Requester"


def test_unknown_requester_is_missing_and_flagged():
    r = evaluate(facts(requester_found=False, department=None, budget_status="unavailable"))
    assert "requester_unknown" in r.flag_codes
    assert r.deterministic_recommendation == Recommendation.REQUEST_MORE_INFO


def test_injection_forces_escalation():
    r = evaluate(facts(injection_hits=[{"location": "request.business_justification", "pattern": "x", "excerpt": "y"}]))
    assert "prompt_injection_detected" in r.flag_codes and r.handoff_required
    assert r.deterministic_recommendation == Recommendation.ESCALATE_TO_HUMAN


def test_limited_use_approval_does_not_cover_sensitive_data():
    r = evaluate(facts(limited_use_matches=["SW009"], data_access_level="customer_pii", data_access_raw="customer_pii"))
    assert "limited_use_approval" in r.flag_codes


def test_overlap_is_low_severity_and_not_a_rejection():
    r = evaluate(facts(catalog_matches=[{"software_id": "SW003", "product_name": "TaskFlow", "reasons": ["same_vendor"]}]))
    assert "existing_tool_overlap" in r.flag_codes
    assert r.deterministic_recommendation == Recommendation.RECOMMEND_APPROVE


def test_every_rule_id_is_reachable():
    """Union of rule IDs produced across scenarios covers rules.yaml (POL-11 is the human-authority principle)."""
    scenarios = [
        facts(), facts(amount_usd=5000), facts(amount_usd=20000, budget_effective_available=10**6),
        facts(amount_usd=30000, budget_effective_available=10**6), facts(budget_status="exceeded"),
        facts(budget_status="no_record"),
        facts(catalog_matches=[{"software_id": "S", "product_name": "P", "reasons": ["same_category"]}]),
        facts(data_access_level="source_code", data_access_raw="source_code", integrations=["AWS"]),
        facts(data_access_level=None, data_access_raw="unknown", missing=["data_access_level"]),
        facts(vendor_effective_status="not_completed"), facts(vendor_effective_status="expired"),
        facts(vendor_conflicts=[{"field": "f", "registry": "a", "service": "b"}]),
        facts(vendor_lookup="unavailable", vendor_effective_status="unverified"),
        facts(vendor_effective_status="rejected"),
        facts(data_access_level="customer_pii", data_access_raw="customer_pii", vendor_stores_outside_region=True,
              limited_use_matches=["SW"]),
        facts(vendor_is_new=True, amount_usd=12000, vendor_legal_terms="Draft", budget_effective_available=10**6),
        facts(injection_hits=[{"location": "l", "pattern": "p", "excerpt": "e"}]),
        facts(requester_id=None, vendor_name=None, product_name=None, amount_usd=None, user_count=None, purpose=None,
              integrations=None, missing=[f.field for f in RULES.required_fields]),
    ]
    seen: set[str] = set()
    for f in scenarios:
        r = evaluate(f)
        seen |= {a.rule_id for a in r.approvals} | {x.rule_id for x in r.flags if x.rule_id}
        seen |= {m.rule_id for m in r.missing_fields} | {b.rule_id for b in r.blocks} | {v.rule_id for v in r.reviews}
    assert RULES.all_rule_ids() - {"POL-11.HUMAN"} <= seen, RULES.all_rule_ids() - seen
