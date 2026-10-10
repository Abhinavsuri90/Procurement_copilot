"""FastAPI app: JSON API for the copilot plus the single-page UI (served from ./static)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from src.config import ROOT, get_settings
from src.data_access import default_repository
from src.obs import configure as configure_logging
from src.obs import log_event
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
configure_logging()


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


Short = Annotated[str, StringConstraints(max_length=200)]


class NewRequest(BaseModel):
    """Bounded input: oversized fields are rejected with 422 instead of being stored and sent to the model."""

    requester_id: str = Field(min_length=1, max_length=32)
    product_name: str = Field(min_length=1, max_length=200)
    vendor_name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    annual_cost_usd: float | None = Field(default=None, ge=0, le=100_000_000)
    user_count: int | None = Field(default=None, gt=0, le=1_000_000)
    business_justification: str | None = Field(default=None, max_length=4000)
    data_access_level: str | None = Field(default=None, max_length=80)
    requested_integrations: list[Short] | None = Field(default=None, max_length=20)
    urgency: str | None = Field(default="normal", max_length=20)


class HumanAction(BaseModel):
    action: Literal["approve", "reject", "request_info", "escalate"]
    reviewer_role: str = Field(max_length=40)
    reason: str | None = Field(default=None, max_length=2000)
    exception_reason: str | None = Field(default=None, max_length=2000)


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


class Progress:
    """Live events of one background analysis, read by the UI while the run is in flight."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.error: str | None = None
        self._lock = threading.Lock()

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append({**event, "t_ms": round((time.perf_counter() - self._t0) * 1000)})

    def start(self) -> None:
        self._t0 = time.perf_counter()

    def snapshot(self, after: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            return self.events[after:], len(self.events)


PROGRESS: dict[str, Progress] = {}
MAX_TRACKED_RUNS = 200


def _validated(request_id: str, arch: str, fault: str | None) -> tuple[dict[str, Any], str]:
    raw = get_request_or_404(request_id)
    try:
        arch = normalize_arch(arch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if fault not in (None, "", "down", "slow", "flaky"):
        raise HTTPException(400, "fault must be down, slow or flaky")
    return raw, arch


def _run_and_store(request_id: str, raw: dict[str, Any], arch: str, fault: str | None, run_id: str | None = None,
                   on_event: Any = None) -> dict[str, Any]:
    result = analyze(PurchaseRequest.from_raw(raw), arch, case_key=(request_id, representative_run(arch, request_id)),
                     fault=fault or None, on_event=on_event, run_id=run_id)
    decision = result.decision.model_dump(mode="json")
    store().add_run(result.run_id, request_id, arch, decision, result.trace, result.raw_output, result.policy)
    return {"run_id": result.run_id, "decision": decision, "trace": result.trace, "status": store().status(request_id)}


@app.post("/api/requests/{request_id}/analyze")
def run_analysis(request_id: str, arch: str = Query("B"), fault: str | None = Query(None),
                 stream: bool = Query(False)) -> dict[str, Any]:
    """Run the copilot. With `stream=true` the run happens in the background and returns its run_id at once;
    poll /api/runs/{run_id}/progress for live phases, model and tool calls."""
    raw, arch = _validated(request_id, arch, fault)
    if not stream:
        return _run_and_store(request_id, raw, arch, fault)
    run_id = uuid.uuid4().hex[:12]
    progress = Progress(request_id)
    progress.start()
    if len(PROGRESS) >= MAX_TRACKED_RUNS:  # keep memory bounded: forget the oldest finished runs
        for key in [k for k, p in PROGRESS.items() if p.done][: MAX_TRACKED_RUNS // 2]:
            PROGRESS.pop(key, None)
    PROGRESS[run_id] = progress

    def work() -> None:
        try:
            _run_and_store(request_id, raw, arch, fault, run_id=run_id, on_event=progress.add)
        except Exception as exc:  # surfaced to the UI; the request stays unchanged
            progress.error = f"{type(exc).__name__}: {exc}"
            log_event("analysis_failed", run_id=run_id, request_id=request_id, error=progress.error)
        finally:
            progress.done = True

    threading.Thread(target=work, name=f"analysis-{run_id}", daemon=True).start()
    return {"run_id": run_id, "status": "running"}


@app.get("/api/runs/{run_id}/progress")
def run_progress(run_id: str, after: int = Query(0, ge=0)) -> dict[str, Any]:
    progress = PROGRESS.get(run_id)
    if progress is None:
        if store().run(run_id) is not None:  # finished before this process tracked it (e.g. after a restart)
            return {"run_id": run_id, "events": [], "next": after, "done": True, "error": None}
        raise HTTPException(404, f"Unknown run '{run_id}'")
    events, nxt = progress.snapshot(after)
    return {"run_id": run_id, "events": events, "next": nxt, "done": progress.done, "error": progress.error,
            "status": store().status(progress.request_id) if progress.done else "running"}


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
        exception = (body.exception_reason or "").strip()
        if not exception:
            raise HTTPException(422, "Policy blocks exist: approval requires an exception reason")
        is_exception = True
        reason = f"{reason} [exception: {exception}]".strip()
    entry = store().add_action(request_id=request_id, run_id=run["run_id"], action=body.action,
                               reviewer_role=body.reviewer_role, reason=reason or None, ai_recommendation=rec,
                               architecture=decision["meta"]["architecture"], model=decision["meta"]["model"],
                               is_override=int(is_override), is_exception=int(is_exception))
    log_event("human_action", request_id=request_id, run_id=run["run_id"], action=body.action,
              reviewer_role=body.reviewer_role, is_override=is_override, is_exception=is_exception,
              status=store().status(request_id))
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
