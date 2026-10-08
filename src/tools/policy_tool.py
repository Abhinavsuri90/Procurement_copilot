"""`evaluate_policy`: the deterministic policy engine exposed as a tool."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.tools.base import RunContext, ToolFailure, register


class PolicyArgs(BaseModel):
    request_id: str = Field(min_length=1)


@register(
    "evaluate_policy",
    "Run the deterministic procurement-policy engine for this request. It gathers its own facts from the request's "
    "structured fields and the recorded tool outputs (budget, vendor, catalog, history, requester), then returns the "
    "authoritative required approvals and reviews, blocks, risk flags, missing fields, handoff requirement and a "
    "rules-only recommendation, each with a policy rule ID. Its result cannot be overridden.",
    PolicyArgs,
    {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]},
    source="policy_engine:rules.yaml",
)
def evaluate_policy(ctx: RunContext, args: PolicyArgs) -> dict:
    from src.policy.engine import evaluate  # local import: the engine's fact collection uses this registry
    from src.policy.facts import collect_facts

    if args.request_id != ctx.request.request_id:
        raise ToolFailure("WRONG_REQUEST", f"This run analyses {ctx.request.request_id}, not {args.request_id}")
    facts = collect_facts(ctx)
    result = evaluate(facts)
    return {
        "request_id": ctx.request.request_id,
        "as_of": ctx.as_of.isoformat(),
        **result.model_dump(mode="json"),
        "rule_ids": sorted({a.rule_id for a in result.approvals} | {f.rule_id for f in result.flags if f.rule_id}
                           | {m.rule_id for m in result.missing_fields} | {b.rule_id for b in result.blocks}),
        "facts_from_calls": facts.call_ids,
    }
