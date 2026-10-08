"""FastAPI app: JSON API for the copilot plus the single-page UI (served from ./static)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import ROOT, get_settings
from src.data_access import default_repository
from src.orchestrator import analyze, normalize_arch, representative_run
from src.schemas import APPROVER_ROLES, PurchaseRequest, Recommendation
from src.store import Store
from src.vendor_client import VendorClient

STATIC = Path(__file__).with_name("static")
SUMMARY = ROOT / "evals" / "results" / "summary.json"

# Which human actions agree with each AI recommendation; anything else is an override and needs a reason.
CONSISTENT = {
    Recommendation.RECOMMEND_APPROVE.value: {"approve"},
    Recommendation.USE_EXISTING_TOOL.value: {"reject", "request_info"},
    Recommendation.REQUEST_MORE_INFO.value: {"request_info"},
    Recommendation.ESCALATE_TO_HUMAN.value: {"escalate", "request_info"},
    Recommendation.RECOMMEND_REJECT.value: {"reject"},
}

app = FastAPI(title="Procurement Request Copilot", version="1.0")


@lru_cache(maxsize=1)
def store() -> Store:
    return Store(get_settings().db_path)


def all_requests() -> dict[str, dict[str, Any]]:
    return {**default_repository().requests, **store().created_requests()}


def get_request_or_404(request_id: str) -> dict[str, Any]:
    raw = all_requests().get(request_id)
    if raw is None:
        raise HTTPException(404, f"Unknown request '{request_id}'")
    return raw


class NewRequest(BaseModel):
    requester_id: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    vendor_name: str = Field(min_length=1)
    category: str | None = None
    annual_cost_usd: float | None = Field(default=None, ge=0)
    user_count: int | None = Field(default=None, gt=0)
    business_justification: str | None = None
    data_access_level: str | None = None
    requested_integrations: list[str] | None = None
    urgency: str | None = "normal"


class HumanAction(BaseModel):
    action: Literal["approve", "reject", "request_info", "escalate"]
    reviewer_role: str
    reason: str | None = None
    exception_reason: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    vendor_ok = VendorClient(s.vendor_service_url, timeout_s=1.0).health()
    return {"app": "ok", "vendor_service": {"url": s.vendor_service_url, "ok": vendor_ok},
            "llm": {"configured": s.llm_configured, "model": s.llm_model or None, "mode": s.llm_mode,
                    "provider": urlparse(s.llm_base_url).hostname},
            "as_of": s.as_of.isoformat(), "roles": APPROVER_ROLES}


@app.get("/api/requests")
def list_requests() -> list[dict[str, Any]]:
    out = []
    for rid, raw in all_requests().items():
        run = store().latest_run(rid)
        out.append({**raw, "status": store().status(rid),
                    "recommendation": run["decision"]["recommendation"] if run else None,
                    "source": "starter" if rid in default_repository().requests else "new"})
    return out


@app.post("/api/requests", status_code=201)
def create_request(body: NewRequest) -> dict[str, Any]:
    payload = {"request_id": store().next_request_id(), **body.model_dump()}
    store().add_request(payload)
    return payload


@app.get("/api/requests/{request_id}")
def request_detail(request_id: str) -> dict[str, Any]:
    raw = get_request_or_404(request_id)
    run = store().latest_run(request_id)
    return {"request": raw, "status": store().status(request_id),
            "latest_run": {k: run[k] for k in ("run_id", "architecture", "created_at", "decision")} if run else None,
            "runs": store().runs_for(request_id), "audit": store().actions_for(request_id),
            "signoffs": {k: v for k, v in store().signoffs(request_id, run).items() if k != "actions"} if run else None}


@app.post("/api/requests/{request_id}/analyze")
def run_analysis(request_id: str, arch: str = Query("B"), fault: str | None = Query(None)) -> dict[str, Any]:
    raw = get_request_or_404(request_id)
    try:
        arch = normalize_arch(arch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if fault not in (None, "", "down", "slow", "flaky"):
        raise HTTPException(400, "fault must be down, slow or flaky")
    result = analyze(PurchaseRequest.from_raw(raw), arch, case_key=(request_id, representative_run(arch, request_id)),
                     fault=fault or None)
    decision = result.decision.model_dump(mode="json")
    store().add_run(result.run_id, request_id, arch, decision, result.trace, result.raw_output, result.policy)
    return {"run_id": result.run_id, "decision": decision, "trace": result.trace, "status": store().status(request_id)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = store().run(run_id)
    if run is None:
        raise HTTPException(404, f"Unknown run '{run_id}'")
    return run


@app.post("/api/requests/{request_id}/actions", status_code=201)
def human_action(request_id: str, body: HumanAction) -> dict[str, Any]:
    """Record a human decision. Only this endpoint changes a request's status - the AI never does."""
    get_request_or_404(request_id)
    if body.reviewer_role not in APPROVER_ROLES:
        raise HTTPException(400, f"reviewer_role must be one of {APPROVER_ROLES}")
    run = store().latest_run(request_id)
    if run is None:
        raise HTTPException(409, "Run the copilot before recording a decision")
    decision = run["decision"]
    rec = decision["recommendation"]
    reason = (body.reason or "").strip()
    status = store().status(request_id)
    if status in ("Approved", "Rejected"):
        raise HTTPException(409, f"Request is closed ({status}); run the copilot again to reopen it")
    signoffs = store().signoffs(request_id, run)
    if body.action == "approve":
        if body.reviewer_role not in signoffs["required"]:
            raise HTTPException(422, f"{body.reviewer_role} is not a required approver for this request "
                                     f"(required: {', '.join(signoffs['required']) or 'none'})")
        if body.reviewer_role in signoffs["approved"]:
            raise HTTPException(409, f"{body.reviewer_role} has already approved this analysis")
    is_override = body.action not in CONSISTENT[rec]
    if is_override and not reason:
        raise HTTPException(422, f"'{body.action}' overrides the AI recommendation '{rec}': a written reason is required")
    blocked = [a for a in run["policy"].get("blocks", [])] if run.get("policy") else []
    is_exception = False
    if body.action == "approve" and blocked:
        if not (body.exception_reason or "").strip():
            raise HTTPException(422, "Policy blocks exist: approval requires an exception reason")
        is_exception = True
        reason = f"{reason} [exception: {body.exception_reason.strip()}]".strip()
    entry = store().add_action(request_id=request_id, run_id=run["run_id"], action=body.action,
                               reviewer_role=body.reviewer_role, reason=reason or None, ai_recommendation=rec,
                               architecture=decision["meta"]["architecture"], model=decision["meta"]["model"],
                               is_override=int(is_override), is_exception=int(is_exception))
    return {"action": entry, "status": store().status(request_id),
            "signoffs": {k: v for k, v in store().signoffs(request_id, run).items() if k != "actions"}}


@app.get("/api/eval/summary")
def eval_summary() -> dict[str, Any]:
    if not SUMMARY.is_file():
        raise HTTPException(404, "No evaluation results yet - run: python -m evals.run_eval")
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
