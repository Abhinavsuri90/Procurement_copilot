"""Builds the policy engine's facts from the structured request and recorded tool outputs.

Facts never come from an agent's restatement. Each canonical tool call (derived from the request itself) is
reused from the run trace if the agent already made it with identical arguments, otherwise executed here by
code. A confused or manipulated agent therefore cannot change what the policy engine sees.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from src.agents.injection import dedupe, scan_value
from src.data_access import norm
from src.policy.rules import Rules, load_rules
from src.tools.base import RunContext, execute, normalize_args
from src.trace import ToolResult

MISSING_MARKERS = {"", "unknown", "tbd", "n/a", "na", "none specified", "not sure", "?"}


class Facts(BaseModel):
    request_id: str
    requester_id: str | None
    product_name: str | None
    vendor_name: str | None
    category: str | None
    amount_usd: float | None
    user_count: int | None
    purpose: str | None
    data_access_level: str | None  # normalised; None when missing/unknown
    data_access_raw: str | None
    integrations: list[str] | None
    missing: list[str] = Field(default_factory=list)  # required request fields that are absent

    requester_found: bool | None = None  # None: lookup failed or not possible
    department: str | None = None

    budget_status: str = "not_checked"  # within | exceeded | unknown_amount | no_record | unavailable
    budget_effective_available: float | None = None

    vendor_lookup: str = "not_checked"  # ok | not_found | unavailable
    vendor_registry_found: bool = False
    vendor_is_new: bool = True
    vendor_procurement_status: str | None = None
    vendor_legal_terms: str | None = None
    vendor_registry_notes: str | None = None
    vendor_effective_status: str = "unknown"
    vendor_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    vendor_expired_items: list[str] = Field(default_factory=list)
    vendor_anomalies: list[str] = Field(default_factory=list)
    vendor_risk_level: str | None = None
    vendor_processes_personal_data: bool | None = None
    vendor_stores_outside_region: bool | None = None

    catalog_matches: list[dict[str, Any]] = Field(default_factory=list)
    limited_use_matches: list[str] = Field(default_factory=list)
    duplicate_purchase_ids: list[str] = Field(default_factory=list)
    injection_hits: list[dict[str, str]] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)

    call_ids: dict[str, str] = Field(default_factory=dict)  # canonical tool -> call_id used for these facts


def normalize_data_class(raw: str | None) -> str | None:
    text = (raw or "").strip().lower()
    if text in MISSING_MARKERS:
        return None
    return re.sub(r"[\s\-/]+", "_", text)


def _canonical(ctx: RunContext, tool: str, args: dict[str, Any]) -> ToolResult:
    canon = normalize_args(tool, args)
    return ctx.trace.find(tool, canon) or execute(ctx, tool, canon, caller="code")


def collect_facts(ctx: RunContext, rules: Rules | None = None) -> Facts:
    rules = rules or load_rules()
    req = ctx.request
    facts = Facts(
        request_id=req.request_id, requester_id=req.requester_id, product_name=req.product_name,
        vendor_name=req.vendor_name, category=req.category, amount_usd=req.annual_cost_usd,
        user_count=req.user_count,
        purpose=req.business_justification if (req.business_justification or "").strip() else None,
        data_access_level=normalize_data_class(req.data_access_level), data_access_raw=req.data_access_level,
        integrations=req.requested_integrations, parse_warnings=list(req.parse_warnings),
    )
    present = {"requester_id": facts.requester_id, "vendor_name": facts.vendor_name,
               "product_name": facts.product_name, "annual_cost_usd": facts.amount_usd,
               "user_count": facts.user_count, "business_justification": facts.purpose,
               "data_access_level": facts.data_access_level, "requested_integrations": facts.integrations}
    facts.missing = [f.field for f in rules.required_fields if present.get(f.field) is None]

    # Requester -> department
    if req.requester_id:
        profile = _canonical(ctx, "get_requester_profile", {"employee_id": req.requester_id})
        facts.call_ids["get_requester_profile"] = profile.call_id
        if profile.ok:
            facts.requester_found = True
            facts.department = profile.data["employee"]["department"]
        elif profile.error and profile.error.code == "NOT_FOUND":
            facts.requester_found = False

    # Budget
    if facts.department:
        args: dict[str, Any] = {"department": facts.department}
        if facts.amount_usd is not None:
            args["amount_usd"] = facts.amount_usd
        budget = _canonical(ctx, "check_budget", args)
        facts.call_ids["check_budget"] = budget.call_id
        if budget.ok:
            facts.budget_effective_available = budget.data["effective_available_usd"]
            within = budget.data["within_budget"]
            facts.budget_status = "unknown_amount" if within is None else ("within" if within else "exceeded")
        else:
            facts.budget_status = "no_record" if budget.error and budget.error.code == "NOT_FOUND" else "unavailable"
    else:
        facts.budget_status = "unavailable"

    # Vendor
    if req.vendor_name:
        vendor = _canonical(ctx, "get_vendor_risk", {"vendor_name": req.vendor_name})
        facts.call_ids["get_vendor_risk"] = vendor.call_id
        data = vendor.data or {}
        registry = data.get("registry_record")
        service = data.get("risk_service", {})
        facts.vendor_lookup = service.get("status", "unavailable") if data else "unavailable"
        facts.vendor_registry_found = registry is not None
        facts.vendor_is_new = data.get("is_new_vendor", True)
        facts.vendor_procurement_status = (registry or {}).get("procurement_status")
        facts.vendor_legal_terms = (registry or {}).get("legal_terms_status")
        facts.vendor_registry_notes = (registry or {}).get("notes")
        facts.vendor_effective_status = data.get("effective_status", "unverified")
        facts.vendor_conflicts = data.get("conflicts", [])
        facts.vendor_expired_items = data.get("expired_items", [])
        facts.vendor_anomalies = data.get("anomalies", [])
        if facts.vendor_lookup == "ok":
            facts.vendor_risk_level = service.get("risk_level")
            facts.vendor_processes_personal_data = service.get("processes_personal_data")
            facts.vendor_stores_outside_region = service.get("stores_data_outside_region")

    # Existing tools (same product / vendor / category). Fit is the agent's judgement; code only surfaces.
    if req.product_name or req.category:
        catalog_args = {"query": req.product_name or req.category, "category": req.category,
                        "vendor_name": req.vendor_name}
        catalog = _canonical(ctx, "search_existing_tools", {k: v for k, v in catalog_args.items() if v})
        facts.call_ids["search_existing_tools"] = catalog.call_id
        markers = [m.lower() for m in rules.ai_tools["limited_use_markers"]]
        for match in (catalog.data or {}).get("matches", []):
            structural = [r for r in match["match_reasons"] if r in ("same_vendor", "same_product", "same_category")]
            if structural and "approved" in norm(match.get("status")):
                facts.catalog_matches.append({"software_id": match["software_id"],
                                              "product_name": match["product_name"], "reasons": structural,
                                              "status": match.get("status"), "scope": match.get("scope"),
                                              "licensed_seats": match.get("licensed_seats")})
            text = f"{match.get('status')} {match.get('notes')}".lower()
            if "same_vendor" in match["match_reasons"] and any(m in text for m in markers):
                facts.limited_use_matches.append(match["software_id"])
    if any(m in (facts.vendor_registry_notes or "").lower() for m in rules.ai_tools["limited_use_markers"]):
        facts.limited_use_matches.append(f"registry:{facts.vendor_name}")

    # Purchase history with this vendor: exact same product bought in the last year = possible duplicate.
    if req.vendor_name:
        history = _canonical(ctx, "get_purchase_history", {"vendor_name": req.vendor_name})
        facts.call_ids["get_purchase_history"] = history.call_id
        for p in (history.data or {}).get("purchases", []):
            bought = _date(p.get("purchase_date"))
            if (norm(p.get("product_name")) == norm(req.product_name) and bought
                    and 0 <= (ctx.as_of - bought).days < 365):
                facts.duplicate_purchase_ids.append(p["purchase_id"])

    # Injection: request text + every data-tool output this run has seen (agent-called or canonical).
    hits = scan_value(req.public_dict(), "request")
    for result in ctx.trace.tool_results.values():
        if result.tool != "evaluate_policy" and result.data is not None:
            hits += scan_value(result.data, f"{result.tool}:{result.call_id}")
    facts.injection_hits = dedupe(hits)
    return facts


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
