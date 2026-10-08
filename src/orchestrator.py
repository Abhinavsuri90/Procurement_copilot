"""Runs one analysis end to end: context -> agent(s) -> policy recomputation -> guardrails -> Decision.

Architectures share everything except orchestration (controlled experiment):
  A  single agent with all tools                      (src/agents/single_agent.py)
  B  analyst -> code handoff -> policy/risk reviewer   (src/agents/staged.py)
  R  rules only, no LLM - the reference row in the evaluation
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.agents.guardrails import deterministic_decision, finalize
from src.agents.llm import Cassette, CassetteMismatch, LLMSession, OpenAICompatBackend
from src.config import Settings, get_settings
from src.data_access import Repository
from src.policy.engine import PolicyResult, evaluate
from src.policy.facts import Facts, collect_facts
from src.runtime import make_context
from src.schemas import Decision, PurchaseRequest
from src.tools.base import RunContext, execute
from src.trace import ToolResult

ARCH_ALIASES = {"a": "A", "single": "A", "b": "B", "staged": "B", "r": "R", "rules": "R"}
ARCH_LABELS = {"A": "A-single-agent", "B": "B-staged-two-agent", "R": "R-rules-only"}


def normalize_arch(value: str) -> str:
    try:
        return ARCH_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown architecture '{value}' (use A/single, B/staged or R/rules)") from exc


@dataclass
class RunResult:
    run_id: str
    decision: Decision
    trace: list[dict[str, Any]]
    raw_output: dict[str, Any] | None = None  # pre-guardrail agent output (A: decision draft, B: both stages)
    policy: dict[str, Any] = field(default_factory=dict)


def _record_request(ctx: RunContext) -> None:
    """The request itself becomes tool result c0 so evidence about it can be cited and checked."""
    ctx.trace.add_tool(ToolResult(call_id="c0", tool="purchase_request", args={"request_id": ctx.request.request_id},
                                  ok=True, data=ctx.request.public_dict(), source="request_intake", caller="code"))


def _policy(ctx: RunContext) -> tuple[Facts, PolicyResult, str]:
    """Authoritative policy result + the trace entry evidence can cite for it."""
    args = {"request_id": ctx.request.request_id}
    call = ctx.trace.find("evaluate_policy", args)
    facts = collect_facts(ctx)
    if call is None or not call.ok:
        call = execute(ctx, "evaluate_policy", args, caller="code")
    return facts, evaluate(facts), call.call_id


def make_session(settings: Settings, ctx: RunContext, arch: str, case_key: tuple[str, int] | None,
                 backend: Any = None) -> tuple[LLMSession | None, str | None]:
    """Pick live / record / replay. Returns (None, reason) when no model can be used for this run.

    Without an API key, a request that has a recorded run (the committed eval cassettes) is replayed, so the
    product still demonstrates the agent offline; anything else gets the deterministic-only decision.
    """
    cassette = Cassette.for_key(settings.cassette_dir, arch, *case_key) if case_key else None
    mode = settings.llm_mode
    if mode == "replay" or (mode == "live" and backend is None and not settings.llm_configured):
        if cassette is not None and cassette.exists():
            cassette.load()
            return LLMSession(None, cassette.model or settings.llm_model, settings.llm_temperature, ctx.trace,
                              mode="replay", cassette=cassette), None
        return None, "ai_unavailable: no LLM key configured and no recording for this request"
    backend = backend or OpenAICompatBackend(settings)
    return LLMSession(backend, backend.model, settings.llm_temperature, ctx.trace, mode=mode,
                      cassette=cassette if mode == "record" else None), None


def analyze(request: PurchaseRequest, architecture: str = "A", *, repo: Repository | None = None,
            settings: Settings | None = None, fault: str | None = None, http: httpx.Client | None = None,
            case_key: tuple[str, int] | None = None, backend: Any = None) -> RunResult:
    """Analyse one request. `case_key=(case_id, run)` selects the cassette used for record/replay."""
    settings = settings or get_settings()
    arch = normalize_arch(architecture)
    ctx = make_context(request, repo=repo, settings=settings, fault=fault, http=http)
    _record_request(ctx)
    start = time.perf_counter()
    warnings: list[str] = list(request.parse_warnings)
    raw: dict[str, Any] | None = None
    session = None
    model = "rules-only"

    if arch == "R":
        facts, policy, policy_cid = _policy(ctx)
        decision = deterministic_decision(ctx, facts, policy, policy_cid, architecture=ARCH_LABELS[arch], model=model)
    else:
        session, unavailable = make_session(settings, ctx, arch, case_key, backend)
        model = session.model if session else (settings.llm_model or "none")
        draft, failure = None, unavailable
        if session is not None:
            try:
                draft, raw, failure = _run_agents(arch, ctx, session)
            except CassetteMismatch as exc:
                failure = f"ai_unavailable: {exc}"
                warnings.append(str(exc))
        facts, policy, policy_cid = _policy(ctx)
        if draft is None:
            decision = deterministic_decision(ctx, facts, policy, policy_cid, architecture=ARCH_LABELS[arch],
                                              model=model, failsafe=failure or "agent_output_invalid")
            if failure:
                warnings.append(failure)
        else:
            decision = finalize(ctx, draft, facts, policy, policy_cid, architecture=ARCH_LABELS[arch], model=model)

    meta = decision.meta
    meta.llm_calls = len(ctx.trace.llm_calls)
    meta.tool_calls = sum(1 for r in ctx.trace.tool_results.values() if r.tool != "purchase_request")
    meta.tool_names = [n for n in ctx.trace.tool_names if n != "purchase_request"]
    meta.tokens_in, meta.tokens_out = ctx.trace.tokens
    meta.replayed = bool(session and session.replaying)
    meta.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    if meta.replayed and session.cassette and session.cassette.run_latency_ms is not None:
        meta.latency_ms = session.cassette.run_latency_ms  # report the recorded latency, not the replay's
    meta.warnings = warnings
    if session is not None and session.mode == "record" and session.cassette is not None:
        session.cassette.run_latency_ms = meta.latency_ms
        session.cassette.save()
    return RunResult(run_id=uuid.uuid4().hex[:12], decision=decision, trace=ctx.trace.dump(), raw_output=raw,
                     policy=policy.model_dump(mode="json"))


def _run_agents(arch: str, ctx: RunContext, session: LLMSession):
    if arch == "A":
        from src.agents.single_agent import run_single_agent

        return run_single_agent(ctx, session)
    from src.agents.staged import run_staged

    return run_staged(ctx, session)
