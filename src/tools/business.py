"""Deterministic data tools: requester profile, budget, existing-tool search, vendor risk, purchase history.

Every record returned carries its stable ID (E004, BUDGET:Finance, SW003, V005, RISK:SignalWatch, PO-2501)
so evidence can cite it and the groundedness guardrail can check it.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

from src.data_access import CatalogItem, Employee, VendorRecord, norm
from src.policy.rules import load_rules
from src.tools.base import RunContext, ToolFailure, register
from src.vendor_client import VendorLookup


def _person(e: Employee | None) -> dict | None:
    return None if e is None else e.model_dump()


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- requester
class RequesterArgs(BaseModel):
    employee_id: str = Field(min_length=1)


@register(
    "get_requester_profile",
    "Look up the requesting employee: department, level, country, manager and department head (approvers). "
    "Deterministic lookup in the HR directory.",
    RequesterArgs,
    {"type": "object", "properties": {"employee_id": {"type": "string", "description": "e.g. E004"}},
     "required": ["employee_id"]},
    source="hr_directory:employees.csv",
)
def get_requester_profile(ctx: RunContext, args: RequesterArgs) -> dict:
    employee = ctx.repo.employee(args.employee_id)
    if employee is None:
        raise ToolFailure("NOT_FOUND", f"No employee with id '{args.employee_id}'")
    manager = ctx.repo.employee(employee.manager_id)
    head = ctx.repo.department_head(employee.employee_id)
    return {
        "employee": _person(employee),
        "manager": _person(manager),
        "department_head": _person(head),
        "budget_department": employee.department,
        "notes": [] if head else ["No Director-level department head found in the reporting line"],
    }


# ---------------------------------------------------------------- budget
class BudgetArgs(BaseModel):
    department: str = Field(min_length=1)
    amount_usd: float | None = Field(default=None, ge=0)


@register(
    "check_budget",
    "Compare an annual amount with the department's available software budget (policy section 2). "
    "All arithmetic is done in code. Omit amount_usd if the request has no cost.",
    BudgetArgs,
    {"type": "object",
     "properties": {"department": {"type": "string", "description": "Requester's department, e.g. Finance"},
                    "amount_usd": {"type": "number", "description": "Annual cost of the request in USD"}},
     "required": ["department"]},
    source="finance:department_budgets.csv",
)
def check_budget(ctx: RunContext, args: BudgetArgs) -> dict:
    budget = ctx.repo.budget(args.department)
    if budget is None:
        raise ToolFailure("NOT_FOUND", f"No software budget record for department '{args.department}'")
    annual, committed, available = budget.annual_software_budget_usd, budget.committed_usd, budget.available_usd
    computed = annual - committed if annual is not None and committed is not None else None
    consistent = computed is not None and available is not None and abs(computed - available) < 0.005
    # If the snapshot's arithmetic disagrees with itself, use the smaller figure (conservative).
    candidates = [v for v in (available, computed) if v is not None]
    effective = min(candidates) if candidates else None
    data = {
        "record_id": budget.record_id,
        "department": budget.department,
        "annual_software_budget_usd": annual,
        "committed_usd": committed,
        "available_usd": available,
        "arithmetic_consistent": consistent,
        "effective_available_usd": effective,
        "requested_amount_usd": args.amount_usd,
        "within_budget": None,
        "remaining_after_usd": None,
        "utilisation_after_pct": None,
    }
    if args.amount_usd is not None and effective is not None:
        data["within_budget"] = args.amount_usd <= effective
        data["remaining_after_usd"] = round(effective - args.amount_usd, 2)
        if annual:
            data["utilisation_after_pct"] = round(100 * (annual - effective + args.amount_usd) / annual, 1)
    return data


# ---------------------------------------------------------------- existing tools
STOPWORDS = {"a", "an", "and", "the", "for", "to", "of", "in", "on", "with", "our", "team", "teams", "tool", "tools",
             "need", "needs", "new", "pro", "plus", "enterprise", "business", "advanced", "add", "software"}
SYNONYMS = [
    {"task", "tasks", "project", "projects", "tracker", "tracking", "kanban", "planning", "management"},
    {"dashboard", "dashboards", "bi", "analytics", "reporting", "reports", "kpi", "metrics"},
    {"signature", "signatures", "esignature", "e-signature", "signing", "sign", "agreements"},
    {"wiki", "knowledge", "docs", "documentation", "notes", "base"},
    {"design", "creative", "templates", "template", "graphics", "prototyping", "brand", "campaign"},
    {"monitoring", "observability", "incident", "incidents", "alerting", "telemetry", "logs"},
    {"coding", "code", "developer", "programming", "copilot", "assistant"},
    {"support", "ticket", "tickets", "ticketing", "helpdesk", "customer"},
    {"ai", "llm", "assistant", "chatbot", "genai", "general"},
]


def _tokens(text: str | None) -> set[str]:
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", (text or "").lower()) if w not in STOPWORDS and len(w) > 1}
    expanded = set(words)
    for group in SYNONYMS:
        if words & group:
            expanded |= group
    return expanded


class CatalogArgs(BaseModel):
    query: str = Field(min_length=1, description="Capability or product being requested")
    category: str | None = None
    vendor_name: str | None = None


@register(
    "search_existing_tools",
    "Search the approved software catalog for tools that may already cover the need: same product, same vendor, "
    "same category or overlapping capabilities. Deterministic keyword/category/synonym ranking; you judge real fit.",
    CatalogArgs,
    {"type": "object",
     "properties": {"query": {"type": "string", "description": "Capability or product, e.g. 'task tracker'"},
                    "category": {"type": "string"}, "vendor_name": {"type": "string"}},
     "required": ["query"]},
    source="catalog:software_catalog.csv",
)
def search_existing_tools(ctx: RunContext, args: CatalogArgs) -> dict:
    query_tokens = _tokens(args.query) | _tokens(args.category)
    results = []
    for item in ctx.repo.catalog:
        score, reasons = _score(item, args, query_tokens)
        if score >= 2:
            results.append({**item.model_dump(), "match_score": score, "match_reasons": reasons})
    results.sort(key=lambda r: (-r["match_score"], r["software_id"]))
    return {"query": args.query, "matches": results[:5], "catalog_size": len(ctx.repo.catalog)}


def _score(item: CatalogItem, args: CatalogArgs, query_tokens: set[str]) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if args.vendor_name and norm(args.vendor_name) == norm(item.vendor_name):
        score += 5
        reasons.append("same_vendor")
    product = norm(item.product_name)
    if product and (product in norm(args.query) or norm(args.query) in product):
        score += 5
        reasons.append("same_product")
    if args.category and norm(args.category) == norm(item.category):
        score += 4
        reasons.append("same_category")
    overlap = query_tokens & (_tokens(item.product_name) | _tokens(item.category) | _tokens(item.notes))
    if overlap:
        score += min(len(overlap), 4)
        reasons.append("keyword_overlap:" + ",".join(sorted(overlap)[:6]))
    return score, reasons


# ---------------------------------------------------------------- vendor risk
STATUS_RANK = {"approved": 1, "unknown": 2, "not_completed": 3, "unverified": 4, "expired": 5, "rejected": 6}


def normalize_status(raw: object) -> str:
    text = norm(str(raw)) if raw is not None else ""
    if text in {"approved", "approve", "current", "passed", "cleared", "active"}:
        return "approved"
    if "expire" in text or text == "stale":
        return "expired"
    if any(w in text for w in ("reject", "fail", "block", "denied", "terminated")):
        return "rejected"
    if any(w in text for w in ("pending", "not_completed", "not completed", "in progress", "in_progress", "review",
                               "draft", "incomplete")):
        return "not_completed"
    return "unknown"


class VendorArgs(BaseModel):
    vendor_name: str = Field(min_length=1)


@register(
    "get_vendor_risk",
    "Get the vendor's security/risk status: calls the external vendor-risk service and joins it with the internal "
    "vendor registry. Code computes expiry (365-day validity as of the policy reference date), conflicts between "
    "the two sources, and the most conservative effective status. Returns ok=false if the service is unavailable.",
    VendorArgs,
    {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]},
    source="vendor_risk_api+vendor_registry",
)
def get_vendor_risk(ctx: RunContext, args: VendorArgs) -> dict:
    registry = ctx.repo.vendor(args.vendor_name)
    lookup = ctx.vendor.get_vendor_risk(args.vendor_name, fault=ctx.fault)
    data = assess_vendor(args.vendor_name, registry, lookup, ctx.as_of, load_rules().security.assessment_validity_days)
    if lookup.status == "unavailable":
        raise ToolFailure("VENDOR_SERVICE_UNAVAILABLE",
                          f"Vendor-risk service unavailable after {lookup.attempts} attempt(s): {lookup.error}",
                          retryable=True, data=data)
    return data


def assess_vendor(name: str, registry: VendorRecord | None, lookup: VendorLookup, as_of: date,
                  validity_days: int) -> dict:
    """Join registry + service, detect conflicts and expiry. Pure function of its inputs."""
    service = lookup.record if lookup.status == "ok" else None
    reg_status = normalize_status(registry.security_status) if registry else None
    svc_status = normalize_status(service.get("security_review_status")) if service else None
    reg_date = _date(registry.security_review_date) if registry else None
    svc_date = _date(service.get("last_review_date")) if service else None

    conflicts: list[dict] = []
    if registry and service:
        if reg_status != svc_status:
            conflicts.append({"field": "security_status", "registry": registry.security_status,
                              "service": service.get("security_review_status")})
        if reg_date != svc_date and (reg_date or svc_date):
            conflicts.append({"field": "review_date", "registry": registry.security_review_date,
                              "service": service.get("last_review_date")})
    if registry and lookup.status == "not_found" and reg_status == "approved":
        conflicts.append({"field": "assessment_record", "registry": registry.security_status,
                          "service": "no record"})

    # Conflicting dates: the older one decides expiry (most conservative).
    dates = [d for d in (reg_date, svc_date) if d]
    review_date = min(dates) if dates else None
    days_since = (as_of - review_date).days if review_date else None
    anomalies = []
    if days_since is not None and days_since < 0:
        anomalies.append(f"review date {review_date} is after the reference date {as_of}")
    # Assumption A3: an assessment is current for days 0..364 after review; on day 365 it has expired.
    expired_by_date = days_since is not None and days_since >= validity_days
    expired_items = []
    if expired_by_date:
        expired_items.append(f"security assessment reviewed {review_date} is {days_since} days old "
                             f"(validity {validity_days} days)")
    for source, status in (("registry", reg_status), ("service", svc_status)):
        if status == "expired":
            expired_items.append(f"{source} reports security review status 'expired'")

    statuses = [s for s in (reg_status, svc_status) if s]
    if expired_by_date:
        statuses.append("expired")
    if lookup.status == "unavailable":
        statuses.append("unverified")
    if anomalies:
        statuses.append("unverified")
    effective = max(statuses, key=STATUS_RANK.__getitem__) if statuses else "unknown"

    risk_service: dict = {"status": lookup.status, "attempts": lookup.attempts}
    if service:
        risk_service.update({"record_id": f"RISK:{service.get('vendor_name', name)}",
                             **{k: v for k, v in service.items() if k != "vendor_name"}})
    else:
        risk_service["error"] = lookup.error
    return {
        "vendor_name": registry.vendor_name if registry else name,
        "as_of": as_of.isoformat(),
        "registry_record": registry.model_dump() if registry else None,
        "risk_service": risk_service,
        "assessment": {"review_date_used": review_date.isoformat() if review_date else None,
                       "days_since_review": days_since, "validity_days": validity_days,
                       "expired": expired_by_date if review_date else None},
        "conflicts": conflicts,
        "expired_items": expired_items,
        "anomalies": anomalies,
        "effective_status": effective,
        "is_new_vendor": registry is None or normalize_status(registry.procurement_status) != "approved",
    }


# ---------------------------------------------------------------- purchase history
class HistoryArgs(BaseModel):
    department: str | None = None
    vendor_name: str | None = None
    product_name: str | None = None


@register(
    "get_purchase_history",
    "List prior purchases and renewals filtered by department, vendor and/or product (filters are combined with AND). "
    "Flags possible duplicates: the same product bought in the last 365 days.",
    HistoryArgs,
    {"type": "object",
     "properties": {"department": {"type": "string"}, "vendor_name": {"type": "string"},
                    "product_name": {"type": "string"}}},
    source="procurement:purchase_history.csv",
)
def get_purchase_history(ctx: RunContext, args: HistoryArgs) -> dict:
    rows = [
        p for p in ctx.repo.purchases
        if (not args.department or norm(p.department) == norm(args.department))
        and (not args.vendor_name or norm(p.vendor_name) == norm(args.vendor_name))
        and (not args.product_name or _similar(p.product_name, args.product_name))
    ]
    duplicates = []
    for p in rows:
        bought = _date(p.purchase_date)
        recent = bought is not None and 0 <= (ctx.as_of - bought).days < 365
        if recent and args.product_name and _similar(p.product_name, args.product_name):
            duplicates.append({"purchase_id": p.purchase_id, "reason": f"{p.product_name} bought {p.purchase_date}"})
    return {
        "filters": args.model_dump(exclude_none=True),
        "purchases": [{**p.model_dump(), "company_wide": "company-wide" in norm(p.notes)} for p in rows],
        "count": len(rows),
        "total_annual_usd": round(sum(p.annual_amount_usd or 0 for p in rows), 2),
        "possible_duplicates": duplicates,
    }


def _similar(a: str | None, b: str | None) -> bool:
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or x in y or y in x)
