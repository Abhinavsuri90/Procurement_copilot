"""Guardrails: turn an agent's draft into the final Decision. Deterministic and identical for A and B.

The policy result is recomputed here from the request and recorded tool outputs (never from the agent's own
restatement), and the agent's draft can only add to it:
  1 schema        - validated in the loop (one repair round-trip); no valid draft -> fail-safe decision
  2 approvals     - policy approvals always kept; agent additions kept only if they cite an existing rule ID
  3 consistency   - approve with blocks / high-risk flags / missing info is overridden to the policy outcome
  4 escalation    - handoff required whenever the policy floor, the agent, or an override says so
  5 groundedness  - evidence must cite a call_id + record IDs + numbers present in that call's output
  6 missing info  - policy fields ∪ agent gaps, each with a question for the requester
  7 injection     - scanner hits always add prompt_injection_detected and force handoff
  8 no autonomy   - nothing here approves or executes anything; only a human action changes status
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.policy.engine import PolicyResult, next_step_for
from src.policy.facts import Facts
from src.policy.rules import load_rules
from src.schemas import (
    APPROVER_ROLES,
    OWNER_ROLES,
    AgentDecisionDraft,
    Approval,
    Decision,
    DecisionMeta,
    DraftEvidence,
    EvidenceItem,
    HumanHandoff,
    MissingInfo,
    NextStep,
    Override,
    Recommendation,
    RiskFlag,
)
from src.tools.base import RunContext
from src.trace import Trace

_NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")
_CALL_ID = re.compile(r"\b[a-z]\d+(-\d+)?\b")


# ---------------------------------------------------------------- groundedness
def _numbers(text: str) -> set[float]:
    out = set()
    for token in _NUMBER.findall(_CALL_ID.sub(" ", text)):
        try:
            out.add(round(float(token.replace(",", "")), 2))
        except ValueError:
            continue
    return out


def _string_values(node: Any) -> list[str]:
    """Every string value in a JSON-like structure (dict keys excluded)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [v for value in node.values() for v in _string_values(value)]
    if isinstance(node, list):
        return [v for value in node for v in _string_values(value)]
    return []


def _id_present(rid: str, values: list[str]) -> bool:
    """A record ID must look like one (contain a digit or ':') and appear as a whole value or a whole token."""
    if not re.search(r"[0-9:]", rid):
        return False
    token = re.compile(rf"(?<![\w:.-]){re.escape(rid)}(?![\w-]|[.:]\w)")
    return any(v == rid or token.search(v) for v in values)


def check_grounded(item: DraftEvidence, trace: Trace) -> str | None:
    """None if grounded, else the reason it is not."""
    result = trace.tool_results.get(item.call_id.strip())
    if result is None:
        return f"call_id '{item.call_id}' is not in this run's trace"
    if item.source_tool.strip() != result.tool:
        return f"source_tool '{item.source_tool}' does not match {result.call_id} ({result.tool})"
    payload = {"data": result.data, "error": result.error.model_dump() if result.error else None, "args": result.args}
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    values = _string_values(payload)
    missing = [rid for rid in item.record_ids if rid.strip() and not _id_present(rid.strip(), values)]
    if missing:
        return f"record IDs {missing} not in {result.call_id} output"
    if result.ok and not [r for r in item.record_ids if r.strip()]:
        return "no record ID cited"
    unsupported = _numbers(f"{item.claim} {item.value}") - _numbers(blob)
    if unsupported:
        return f"numbers {sorted(unsupported)} not in {result.call_id} output"
    return None


# ---------------------------------------------------------------- deterministic evidence (R, fail-safe, backfill)
def deterministic_evidence(trace: Trace, facts: Facts, policy_call_id: str | None) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []

    def add(call_id: str | None, claim: str, record_ids: list[str], value: str = "") -> None:
        if call_id and call_id in trace.tool_results:
            items.append(EvidenceItem(id=f"D{len(items) + 1}", claim=claim, source_tool=trace.tool_results[call_id].tool,
                                      call_id=call_id, record_ids=[r for r in record_ids if r], value=value,
                                      origin="deterministic"))

    req = trace.tool_results.get("c0")
    if req:
        d = req.data
        add("c0", f"Request {d['request_id']}: {d.get('product_name')} from {d.get('vendor_name')}, annual cost "
            f"{d.get('annual_cost_usd')}, users {d.get('user_count')}, data access '{d.get('data_access_level')}'",
            [d["request_id"]])
    cid = facts.call_ids.get("get_requester_profile")
    if cid and trace.tool_results[cid].ok:
        d = trace.tool_results[cid].data
        e, m, h = d["employee"], d.get("manager") or {}, d.get("department_head") or {}
        add(cid, f"Requester {e['employee_id']} {e['name']} ({e['department']}, {e['level']}); manager "
            f"{m.get('employee_id', 'none')}; department head {h.get('employee_id', 'none')}",
            [e["employee_id"], m.get("employee_id"), h.get("employee_id")])
    cid = facts.call_ids.get("check_budget")
    if cid and trace.tool_results[cid].ok:
        d = trace.tool_results[cid].data
        verdict = {True: "within budget", False: "exceeds the available budget", None: "not checkable (no amount)"}
        add(cid, f"{d['department']} available software budget {d['effective_available_usd']}; requested "
            f"{d['requested_amount_usd']} is {verdict[d['within_budget']]}", [d["record_id"]],
            str(d["effective_available_usd"]))
    cid = facts.call_ids.get("get_vendor_risk")
    if cid:
        r = trace.tool_results[cid]
        d = r.data or {}
        reg = d.get("registry_record") or {}
        svc = d.get("risk_service") or {}
        if r.ok:
            claim = (f"{d['vendor_name']}: effective security status '{d['effective_status']}' as of {d['as_of']}; "
                     f"registry status '{reg.get('security_status')}', risk service '{svc.get('security_review_status')}'")
            if d.get("conflicts"):
                claim += f"; conflicts: {', '.join(c['field'] for c in d['conflicts'])}"
        else:
            claim = f"Vendor-risk service unavailable for {d.get('vendor_name', facts.vendor_name)} - status not verified"
        add(cid, claim, [reg.get("vendor_id"), svc.get("record_id")])
    cid = facts.call_ids.get("search_existing_tools")
    if cid and facts.catalog_matches:
        for m in facts.catalog_matches[:3]:
            add(cid, f"Existing approved tool {m['software_id']} {m['product_name']} (scope {m['scope']}, "
                f"{m['licensed_seats']} seats; {'/'.join(m['reasons'])})", [m["software_id"]])
    cid = facts.call_ids.get("get_purchase_history")
    if cid and trace.tool_results[cid].ok and trace.tool_results[cid].data["purchases"]:
        ps = trace.tool_results[cid].data["purchases"]
        add(cid, "Prior purchases with this vendor: " + "; ".join(f"{p['purchase_id']} {p['product_name']} "
            f"({p['department']}, {p['notes']})" for p in ps[:3]), [p["purchase_id"] for p in ps[:3]])
    if policy_call_id and trace.tool_results[policy_call_id].ok:
        d = trace.tool_results[policy_call_id].data
        add(policy_call_id, "Policy engine: required approvals " + ", ".join(
            f"{a['role']} ({a['rule_id']})" for a in d["approvals"]) + f"; rules-only outcome "
            f"{d['deterministic_recommendation']}", sorted({a["rule_id"] for a in d["approvals"]}))
    return items


def _policy_flags(policy: PolicyResult) -> list[RiskFlag]:
    return [RiskFlag(code=f.code, severity=f.severity, detail=f.detail, rule_id=f.rule_id) for f in policy.flags]


def _policy_approvals(policy: PolicyResult) -> list[Approval]:
    return [Approval(role=a.role, reason=a.reason, rule_id=a.rule_id) for a in policy.approvals]


def _policy_missing(policy: PolicyResult) -> list[MissingInfo]:
    return [MissingInfo(field=m.field, question_for_requester=m.question, blocking=m.blocking) for m in policy.missing_fields]


def _handoff_from_policy(policy: PolicyResult, extra: list[str] | None = None) -> HumanHandoff:
    reasons = list(policy.handoff_reasons) + list(extra or [])
    return HumanHandoff(required=bool(reasons), assigned_role=policy.handoff_role, reasons=reasons,
                        decision_needed=_decision_needed(policy.deterministic_recommendation, policy))


def _decision_needed(rec: Recommendation, policy: PolicyResult) -> str:
    return {
        Recommendation.RECOMMEND_APPROVE: "Approve or reject the purchase through the listed approvers",
        Recommendation.USE_EXISTING_TOOL: "Confirm the existing tool meets the need or record why it does not",
        Recommendation.REQUEST_MORE_INFO: "Send the questions to the requester, then re-run the analysis",
        Recommendation.ESCALATE_TO_HUMAN: "Resolve: " + ", ".join(f.code for f in policy.high_flags()) if
        policy.high_flags() else "Review the request manually",
        Recommendation.RECOMMEND_REJECT: "Confirm the policy block or grant a documented exception",
    }[rec]


def allowed_owners(rec: Recommendation, approvals: list[Approval]) -> set[str]:
    """Who can sensibly own the next step for each recommendation."""
    if rec == Recommendation.RECOMMEND_APPROVE:
        return {a.role for a in approvals} | {"Procurement"}
    if rec == Recommendation.USE_EXISTING_TOOL:
        return {"Procurement", "Requester"}
    if rec == Recommendation.REQUEST_MORE_INFO:
        return {"Requester", "Procurement"}
    if rec == Recommendation.RECOMMEND_REJECT:
        return {"Procurement", "Requester"}
    return set(OWNER_ROLES) - {"Requester"}


# ---------------------------------------------------------------- decisions
def deterministic_decision(ctx: RunContext, facts: Facts, policy: PolicyResult, policy_call_id: str | None, *,
                           architecture: str, model: str, failsafe: str | None = None) -> Decision:
    """Rules-only decision (row R), or the fail-safe when the AI is unavailable or its output is invalid."""
    rec = policy.deterministic_recommendation
    flags = _policy_flags(policy)
    extra: list[str] = []
    if failsafe:
        # B prefixes stage names ("analyst llm_error: ..."), so match the cause anywhere in the message.
        code = "ai_unavailable" if any(c in failsafe for c in ("ai_unavailable", "llm_error")) else "agent_output_invalid"
        flags.append(RiskFlag(code=code, severity="high", detail=f"AI analysis unavailable ({failsafe}); "
                              "deterministic checks only", source="deterministic"))
        extra.append(f"{code}: deterministic checks only")
        if rec in (Recommendation.RECOMMEND_APPROVE, Recommendation.USE_EXISTING_TOOL):
            rec = Recommendation.ESCALATE_TO_HUMAN
    handoff = _handoff_from_policy(policy, extra)
    summary = (f"Rules-only assessment: {rec.value.replace('_', ' ')}. "
               f"{len(policy.approvals)} approval(s) required; {len(policy.high_flags())} high-severity flag(s).")
    if failsafe:
        summary = "AI unavailable - deterministic checks only. " + summary
    return Decision(
        request_id=ctx.request.request_id, recommendation=rec, summary=summary,
        evidence=deterministic_evidence(ctx.trace, facts, policy_call_id),
        approvals_required=_policy_approvals(policy), missing_information=_policy_missing(policy),
        risk_flags=flags, next_step=NextStep(**next_step_for(rec, policy)), human_handoff=handoff,
        meta=DecisionMeta(architecture=architecture, model=model, deterministic_only=True),
    )


def finalize(ctx: RunContext, draft: AgentDecisionDraft, facts: Facts, policy: PolicyResult,
             policy_call_id: str | None, *, architecture: str, model: str) -> Decision:
    rules = load_rules()
    valid_rule_ids = rules.all_rule_ids()
    overrides: list[Override] = []
    report: dict[str, Any] = {"raw_recommendation": draft.recommendation.value,
                              "raw_handoff_required": draft.human_handoff.required,
                              "raw_approval_roles": sorted({a.role for a in draft.approvals_required}),
                              "policy_approval_roles": sorted(policy.approval_roles),
                              "deterministic_recommendation": policy.deterministic_recommendation.value}

    # 2 approvals
    approvals = _policy_approvals(policy)
    present = {a.role for a in approvals}
    dropped = []
    for a in draft.approvals_required:
        if a.role in present:
            continue
        if a.role in APPROVER_ROLES and a.rule_id.strip() in valid_rule_ids:
            approvals.append(Approval(role=a.role, reason=a.reason, rule_id=a.rule_id.strip(), source="agent"))
            present.add(a.role)
        else:
            dropped.append({"role": a.role, "rule_id": a.rule_id})
    report["uncited_approvals_dropped"] = dropped
    report["agent_missing_policy_approvals"] = sorted(set(policy.approval_roles) - set(report["raw_approval_roles"]))

    # 5 groundedness
    grounded: list[EvidenceItem] = []
    rejected = []
    for e in draft.evidence:
        reason = check_grounded(e, ctx.trace)
        if reason is None:
            grounded.append(EvidenceItem(**e.model_dump(), origin="agent"))
        else:
            rejected.append({"id": e.id, "claim": e.claim[:160], "reason": reason})
    cited = {e.call_id for e in grounded}
    backfill = [d for d in deterministic_evidence(ctx.trace, facts, policy_call_id) if d.call_id not in cited]
    report.update(evidence_total=len(draft.evidence), evidence_grounded=len(grounded),
                  ungrounded_removed=rejected, deterministic_backfill=len(backfill))

    # flags: deterministic ones can never be removed; agent ones are added
    flags = _policy_flags(policy)
    codes = {f.code for f in flags}
    for f in draft.risk_flags:
        code = re.sub(r"[^a-z0-9_]+", "_", f.code.strip().lower()).strip("_")
        if code and code not in codes:
            flags.append(RiskFlag(code=code, severity=f.severity, detail=f.detail, source="agent",
                                  evidence_ids=f.evidence_ids))
            codes.add(code)

    # 6 missing information
    missing = _policy_missing(policy)
    known = {m.field.lower() for m in missing}
    for m in draft.missing_information:
        if m.field.lower() not in known:
            missing.append(MissingInfo(field=m.field, question_for_requester=m.question_for_requester,
                                       blocking=m.blocking, source="agent"))
            known.add(m.field.lower())

    # 3 consistency
    rec = draft.recommendation
    det = policy.deterministic_recommendation
    if rec == Recommendation.RECOMMEND_APPROVE and det != Recommendation.RECOMMEND_APPROVE:
        overrides.append(Override(field="recommendation", agent_value=rec.value, final_value=det.value,
                                  reason=f"policy outcome is {det.value}: approval is not consistent with "
                                         + (", ".join(f.code for f in policy.high_flags()) or "missing information"
                                            if not policy.blocks else "a policy block")))
        rec = det
    elif rec == Recommendation.RECOMMEND_REJECT and not policy.blocks:
        overrides.append(Override(field="recommendation", agent_value=rec.value,
                                  final_value=Recommendation.ESCALATE_TO_HUMAN.value,
                                  reason="no policy block exists; rejection is a human judgement"))
        rec = Recommendation.ESCALATE_TO_HUMAN
    elif rec == Recommendation.USE_EXISTING_TOOL and not any(e.source_tool == "search_existing_tools" for e in grounded):
        # The agent judged the purchase unnecessary but could not show why: a human decides, never auto-route.
        fallback = det if det != Recommendation.RECOMMEND_APPROVE else Recommendation.ESCALATE_TO_HUMAN
        overrides.append(Override(field="recommendation", agent_value=rec.value, final_value=fallback.value,
                                  reason="no grounded catalog evidence supports the existing-tool redirect"))
        rec = fallback

    # 4 escalation floor + 7 injection
    reasons = list(policy.handoff_reasons)
    if draft.human_handoff.required:
        reasons += [f"agent: {r}" for r in draft.human_handoff.reasons] or ["agent requested human review"]
    if overrides:
        reasons.append("agent recommendation disagreed with policy and was overridden")
    if facts.injection_hits and not any("prompt_injection_detected" in r for r in reasons):
        reasons.append("high risk flag: prompt_injection_detected")
    if rec != Recommendation.RECOMMEND_APPROVE and rec != Recommendation.USE_EXISTING_TOOL and not reasons:
        reasons.append(f"recommendation is {rec.value}")
    required = bool(reasons)
    role = policy.handoff_role if policy.handoff_required else (
        draft.human_handoff.assigned_role if draft.human_handoff.assigned_role in OWNER_ROLES else "Procurement")
    handoff = HumanHandoff(required=required, assigned_role=role, reasons=reasons,
                           decision_needed=draft.human_handoff.decision_needed if not overrides
                           else _decision_needed(rec, policy))

    # next step: the agent's, unless the recommendation changed or its owner does not fit the recommendation
    step = draft.next_step
    if overrides or step.owner_role not in allowed_owners(rec, approvals):
        next_step = NextStep(**next_step_for(rec, policy))
        report["next_step_replaced"] = not overrides
    else:
        next_step = NextStep(action=step.action, owner_role=step.owner_role, detail=step.detail)

    summary = " ".join(re.split(r"(?<=[.!?])\s+", draft.summary.strip())[:3])
    if overrides:
        summary = f"[Guardrail override: {overrides[0].agent_value} -> {overrides[0].final_value}] " + summary

    report["overrides"] = len(overrides)
    report["raw_policy_adherent"] = (not overrides and not report["agent_missing_policy_approvals"]
                                     and (draft.human_handoff.required or not policy.handoff_required))
    return Decision(
        request_id=ctx.request.request_id, recommendation=rec, summary=summary, evidence=grounded + backfill,
        approvals_required=approvals, missing_information=missing, risk_flags=flags, next_step=next_step,
        human_handoff=handoff, overrides=overrides,
        meta=DecisionMeta(architecture=architecture, model=model, guardrails=report),
    )
