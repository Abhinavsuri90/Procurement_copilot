"""Deterministic policy engine: `evaluate(facts, rules) -> PolicyResult`. Pure: no I/O, no LLM, no clock.

This is the CODE layer of the design principle. Thresholds, budget, vendor status, required reviews and the
escalation floor are decided here; the agents interpret and explain, and the guardrails make sure nothing they
say can weaken this result.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from src.policy.facts import Facts
from src.policy.rules import Rules, load_rules
from src.schemas import Recommendation

QUESTIONS = {
    "requester_id": "Who is the requester, and which department's budget will pay for this?",
    "vendor_name": "Which vendor supplies this product?",
    "product_name": "Which product (and edition/plan) are you requesting?",
    "annual_cost_usd": "What is the expected annual cost in USD (or a reasonable annual estimate), including all "
                       "seats and add-ons?",
    "user_count": "How many users or licences do you need?",
    "business_justification": "What business problem will this solve, and why can't an existing approved tool cover it?",
    "data_access_level": "What data will the tool access or store (e.g. none, internal documents, customer PII, "
                         "employee PII, source code, confidential documents, production systems)?",
    "requested_integrations": "Which systems will it integrate with (e.g. SSO, CRM, production cloud accounts), or none?",
}
VENDOR_FLAGS = {"vendor_rejected", "vendor_review_expired", "conflicting_vendor_evidence", "vendor_risk_unavailable",
                "vendor_not_approved", "vendor_assessment_missing", "high_vendor_risk", "limited_use_approval"}


class PolicyApproval(BaseModel):
    role: str
    rule_id: str
    reason: str


class PolicyReview(BaseModel):
    type: str  # security | privacy | legal | finance
    rule_id: str
    reason: str


class PolicyFlag(BaseModel):
    code: str
    severity: str
    detail: str
    rule_id: str | None = None


class PolicyMissing(BaseModel):
    field: str
    rule_id: str
    why: str
    question: str
    blocking: bool = True


class PolicyBlock(BaseModel):
    rule_id: str
    reason: str


class PolicyResult(BaseModel):
    policy_version: str
    request_id: str
    tier_rule_id: str | None = None
    approvals: list[PolicyApproval] = Field(default_factory=list)
    reviews: list[PolicyReview] = Field(default_factory=list)
    blocks: list[PolicyBlock] = Field(default_factory=list)
    flags: list[PolicyFlag] = Field(default_factory=list)
    missing_fields: list[PolicyMissing] = Field(default_factory=list)
    handoff_required: bool = False
    handoff_reasons: list[str] = Field(default_factory=list)
    handoff_role: str = "Procurement"
    deterministic_recommendation: Recommendation = Recommendation.ESCALATE_TO_HUMAN
    next_step: dict[str, str] = Field(default_factory=dict)

    @property
    def approval_roles(self) -> list[str]:
        return [a.role for a in self.approvals]

    @property
    def flag_codes(self) -> set[str]:
        return {f.code for f in self.flags}

    def high_flags(self) -> list[PolicyFlag]:
        return [f for f in self.flags if f.severity in ("high", "critical")]


class _Builder:
    def __init__(self, rules: Rules, request_id: str):
        self.rules = rules
        self.result = PolicyResult(policy_version=rules.policy_version, request_id=request_id)

    def approve(self, role: str, rule_id: str, reason: str) -> None:
        for existing in self.result.approvals:
            if existing.role == role:
                if reason not in existing.reason:
                    existing.reason += f"; {reason}"
                return
        self.result.approvals.append(PolicyApproval(role=role, rule_id=rule_id, reason=reason))

    def review(self, kind: str, rule_id: str, reason: str) -> None:
        self.result.reviews.append(PolicyReview(type=kind, rule_id=rule_id, reason=reason))
        self.approve(kind.capitalize(), rule_id, reason)

    def flag(self, code: str, detail: str, rule_id: str | None = None) -> None:
        for existing in self.result.flags:
            if existing.code == code:
                if detail not in existing.detail:
                    existing.detail += f"; {detail}"
                return
        self.result.flags.append(PolicyFlag(code=code, severity=self.rules.severity(code), detail=detail,
                                            rule_id=rule_id))


def _matches_keyword(text: str, keyword: str) -> bool:
    return re.search(rf"\b{re.escape(keyword)}\b", text, flags=re.IGNORECASE) is not None


def evaluate(facts: Facts, rules: Rules | None = None) -> PolicyResult:
    rules = rules or load_rules()
    b = _Builder(rules, facts.request_id)
    r = b.result
    sec = rules.security.rule_ids

    # §1 Required information
    labels = {f.field: f for f in rules.required_fields}
    for field in facts.missing:
        spec = labels[field]
        r.missing_fields.append(PolicyMissing(field=field, rule_id=spec.rule_id, why=f"{spec.label} is missing",
                                              question=QUESTIONS[field]))
    if facts.requester_found is False:
        r.missing_fields.append(PolicyMissing(field="requester_id", rule_id="POL-1.REQUESTER",
                                              why=f"requester '{facts.requester_id}' is not in the employee directory",
                                              question=QUESTIONS["requester_id"]))
        b.flag("requester_unknown", f"Requester '{facts.requester_id}' not found in the employee directory",
               "POL-1.REQUESTER")
    if r.missing_fields:
        b.flag("missing_information", "Missing: " + ", ".join(m.field for m in r.missing_fields),
               r.missing_fields[0].rule_id)

    # §4 Financial approval thresholds
    if facts.amount_usd is not None:
        for tier in rules.thresholds:
            if tier.max_usd is None or facts.amount_usd <= tier.max_usd:
                r.tier_rule_id = tier.rule_id
                for role in tier.approvers:
                    b.approve(role, tier.rule_id, f"annual amount ${facts.amount_usd:,.2f} is in tier {tier.label}")
                if rules.senior_approver in tier.approvers:
                    b.flag("senior_approval_required", f"{rules.senior_approver} approval required ({tier.label})",
                           tier.rule_id)
                break

    # §2 Budget
    budget = rules.budget
    if facts.budget_status == "exceeded":
        b.approve(budget["exceeded"]["approver"], budget["exceeded"]["rule_id"],
                  "budget exception review: annual cost exceeds the department's available software budget")
        b.result.reviews.append(PolicyReview(type="finance", rule_id=budget["exceeded"]["rule_id"],
                                             reason="budget exception review"))
        b.flag("budget_insufficient", f"${facts.amount_usd:,.2f} requested vs ${facts.budget_effective_available:,.2f} "
               f"available for {facts.department}", budget["exceeded"]["rule_id"])
    elif facts.budget_status in ("no_record", "unavailable") and facts.requester_found is not False:
        b.approve(budget["unverified"]["approver"], budget["unverified"]["rule_id"],
                  "budget could not be verified")
        b.flag("budget_unverified", f"No verifiable software budget for department '{facts.department}'",
               budget["unverified"]["rule_id"])

    # §3 Existing software / overlap (surfaced, never an automatic rejection)
    if facts.catalog_matches:
        listed = ", ".join(f"{m['software_id']} {m['product_name']} ({'/'.join(m['reasons'])})"
                           for m in facts.catalog_matches)
        b.flag("existing_tool_overlap", f"Approved catalog tools overlap: {listed}", rules.overlap["rule_id"])
    if facts.duplicate_purchase_ids:
        b.flag("possible_duplicate_purchase", "Same product bought in the last 365 days: "
               + ", ".join(facts.duplicate_purchase_ids), rules.overlap["rule_id"])

    # Data classification (unknown or unrecognised classes are treated as sensitive - assumption A4)
    dc = facts.data_access_level
    known_safe = dc in rules.non_sensitive_data_classes
    pii = dc is not None and (dc in rules.privacy["pii_data_classes"] or "pii" in dc or "personal" in dc)
    sensitive = dc is not None and (pii or dc in rules.security.data_classes or dc.startswith("production")
                                    or any(w in dc for w in ("secret", "credential", "source", "confidential")))
    unknown = dc is None or not (known_safe or sensitive)

    # §5 Security review
    if sensitive:
        b.review("security", sec["data"], f"data access level '{facts.data_access_raw}' requires security review")
        b.flag("sensitive_data", f"Sensitive data class: {facts.data_access_raw}", sec["data"])
    elif unknown:
        b.review("security", sec["unknown_data"],
                 f"data access level '{facts.data_access_raw or 'missing'}' is unknown; treated as sensitive")
        b.flag("unknown_data_class", f"Data access level '{facts.data_access_raw or 'missing'}' is not recognised",
               sec["unknown_data"])
    risky = [i for i in facts.integrations or [] if any(_matches_keyword(i, k) for k in rules.security.integration_keywords)]
    if risky:
        b.review("security", sec["integration"], f"production/cloud integration requested: {', '.join(risky)}")

    status = facts.vendor_effective_status
    if facts.vendor_name:
        if facts.vendor_lookup == "unavailable":
            b.review("security", sec["unavailable"], "vendor risk could not be verified (service unavailable)")
            b.flag("vendor_risk_unavailable", "Vendor-risk service unavailable; vendor status not verified - no "
                   "favourable status inferred", rules.tool_failure["rule_id"])
        if status == "rejected":
            r.blocks.append(PolicyBlock(rule_id=rules.blocks["rule_id"], reason="vendor security status is rejected"))
            b.flag("vendor_rejected", "Vendor security status is rejected", rules.blocks["rule_id"])
        if status == "expired" or facts.vendor_expired_items:
            b.review("security", sec["expired"], "vendor security assessment is expired")
            b.flag("vendor_review_expired", "; ".join(facts.vendor_expired_items) or "assessment expired", sec["expired"])
        if status in ("not_completed", "unknown") and facts.vendor_lookup != "unavailable":
            b.review("security", sec["assessment"], f"vendor security assessment is {status.replace('_', ' ')}")
            b.flag("vendor_assessment_missing", f"Vendor security assessment status: {status}", sec["assessment"])
        if facts.vendor_conflicts or facts.vendor_anomalies:
            detail = "; ".join([f"{c['field']}: registry={c['registry']} vs service={c['service']}"
                                for c in facts.vendor_conflicts] + facts.vendor_anomalies)
            b.review("security", sec["conflict"], "vendor registry and vendor-risk service disagree")
            b.flag("conflicting_vendor_evidence", detail, sec["conflict"])
        if facts.vendor_is_new:
            b.flag("vendor_not_approved", f"Vendor procurement status is '{facts.vendor_procurement_status or 'not in registry'}'"
                   " - not yet an approved vendor", sec["assessment"])
        if (facts.vendor_risk_level or "").lower() in ("high", "critical"):
            b.flag("high_vendor_risk", f"Vendor risk level: {facts.vendor_risk_level}", sec["assessment"])
    if any(rv.type == "security" for rv in r.reviews):
        b.flag("security_review_required", "; ".join(rv.reason for rv in r.reviews if rv.type == "security"),
               next(rv.rule_id for rv in r.reviews if rv.type == "security"))

    # §6 Privacy review
    personal_or_unknown = pii or unknown
    region_unverified = facts.vendor_stores_outside_region is None
    if pii:
        b.review("privacy", rules.privacy["rule_ids"]["pii"], f"tool will process {facts.data_access_raw}")
    if (sensitive or unknown) and (facts.vendor_stores_outside_region or region_unverified):
        where = "outside the operating region" if facts.vendor_stores_outside_region else "in an unverified region"
        b.review("privacy", rules.privacy["rule_ids"]["region"], f"sensitive data may be stored {where}")
    if any(rv.type == "privacy" for rv in r.reviews):
        b.flag("privacy_review_required", "; ".join(rv.reason for rv in r.reviews if rv.type == "privacy"),
               next(rv.rule_id for rv in r.reviews if rv.type == "privacy"))

    # §7 Legal review
    legal = rules.legal
    if facts.vendor_name and facts.vendor_is_new and facts.amount_usd is not None \
            and facts.amount_usd >= legal["new_vendor_min_usd"]:
        b.review("legal", legal["rule_ids"]["new_vendor_spend"],
                 f"new vendor with annual spend ${facts.amount_usd:,.2f} (>= ${legal['new_vendor_min_usd']:,})")
    if facts.vendor_name and (facts.vendor_legal_terms or "").strip().lower() not in legal["approved_terms"]:
        b.review("legal", legal["rule_ids"]["terms"],
                 f"legal terms status is '{facts.vendor_legal_terms or 'unknown'}', not approved/standard")
    if personal_or_unknown and facts.vendor_stores_outside_region:
        b.review("legal", legal["rule_ids"]["cross_region"], "personal data may be processed outside the region")
        b.flag("cross_region_personal_data", "Vendor stores data outside the operating region and the request "
               f"involves '{facts.data_access_raw or 'unknown'}' data", legal["rule_ids"]["cross_region"])
    if any(rv.type == "legal" for rv in r.reviews):
        b.flag("legal_review_required", "; ".join(rv.reason for rv in r.reviews if rv.type == "legal"),
               next(rv.rule_id for rv in r.reviews if rv.type == "legal"))

    # §8 AI tools / limited-use approvals do not extend to sensitive data classes
    if facts.limited_use_matches and (sensitive or unknown):
        b.flag("limited_use_approval", "Existing approval for this vendor is limited-use "
               f"({', '.join(facts.limited_use_matches)}); it does not cover '{facts.data_access_raw or 'unknown'}' data",
               rules.ai_tools["rule_id"])

    # §9 Untrusted content
    if facts.injection_hits:
        b.flag("prompt_injection_detected", "Instruction-like text in business data was ignored: "
               + "; ".join(f"{h['location']} ({h['pattern']})" for h in facts.injection_hits[:4]),
               rules.injection["rule_id"])

    if facts.parse_warnings:
        b.flag("data_anomaly", "; ".join(facts.parse_warnings))

    _finish(r, facts, sensitive, unknown)
    return r


def _finish(r: PolicyResult, facts: Facts, sensitive: bool, unknown: bool) -> None:
    """Escalation floor, deterministic recommendation and next step."""
    reasons = [f"policy block {bl.rule_id}: {bl.reason}" for bl in r.blocks]
    reasons += [f"{f.severity} risk flag: {f.code}" for f in r.high_flags()]
    if "senior_approval_required" in r.flag_codes:
        reasons.append("senior (CFO) approval threshold reached")
    if sensitive or unknown:
        reasons.append(f"{'sensitive' if sensitive else 'unknown'} data class: {facts.data_access_raw or 'missing'}")
    if any(m.blocking for m in r.missing_fields):
        reasons.append("blocking information is missing")
    r.handoff_reasons = reasons
    r.handoff_required = bool(reasons)

    codes = r.flag_codes
    if r.blocks:
        r.deterministic_recommendation = Recommendation.RECOMMEND_REJECT
    elif any(m.blocking for m in r.missing_fields):
        r.deterministic_recommendation = Recommendation.REQUEST_MORE_INFO
    elif r.high_flags():
        r.deterministic_recommendation = Recommendation.ESCALATE_TO_HUMAN
    else:
        r.deterministic_recommendation = Recommendation.RECOMMEND_APPROVE

    if (codes & VENDOR_FLAGS) or ("security_review_required" in codes and (sensitive or unknown)):
        r.handoff_role = "Security"
    elif "cross_region_personal_data" in codes or "privacy_review_required" in codes:
        r.handoff_role = "Privacy"
    elif codes & {"budget_insufficient", "budget_unverified"}:
        r.handoff_role = "Finance"
    elif "senior_approval_required" in codes:
        r.handoff_role = "CFO"
    else:
        r.handoff_role = "Procurement"
    r.next_step = next_step_for(r.deterministic_recommendation, r)


def next_step_for(rec: Recommendation, r: PolicyResult) -> dict[str, str]:
    roles = r.approval_roles
    if rec == Recommendation.RECOMMEND_REJECT:
        return {"action": "close_request", "owner_role": "Procurement",
                "detail": "Confirm the policy block with the requester: " + "; ".join(b.reason for b in r.blocks)}
    if rec == Recommendation.REQUEST_MORE_INFO:
        return {"action": "request_information", "owner_role": "Requester",
                "detail": "Requester to provide: " + ", ".join(m.field for m in r.missing_fields)}
    if rec == Recommendation.ESCALATE_TO_HUMAN:
        return {"action": "escalate_for_review", "owner_role": r.handoff_role,
                "detail": "Human review needed: " + "; ".join(f.code for f in r.high_flags())}
    if rec == Recommendation.USE_EXISTING_TOOL:
        return {"action": "redirect_to_existing_tool", "owner_role": "Procurement",
                "detail": "Confirm the existing approved tool meets the need and allocate seats"}
    first = "Manager" if roles[:1] == ["Manager"] else ("Department Head" if "Department Head" in roles else "Procurement")
    return {"action": "route_for_approval", "owner_role": first,
            "detail": "Route to approvers in order: " + ", ".join(roles)}
