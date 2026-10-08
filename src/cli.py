"""Command line: `python -m src.cli analyze REQ-1001 --arch A [--fault down] [--json]` and `python -m src.cli list`."""

from __future__ import annotations

import argparse
import json
import sys

from src.data_access import default_repository
from src.orchestrator import analyze, representative_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="Procurement copilot CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("analyze", help="analyse one request")
    run.add_argument("request_id")
    run.add_argument("--arch", default="B", help="B|staged (shipped default), A|single, R|rules")
    run.add_argument("--fault", choices=["down", "slow", "flaky"], help="inject a vendor-service fault")
    run.add_argument("--json", action="store_true", help="print the full decision as JSON")
    run.add_argument("--trace", action="store_true", help="also print the call trace")
    sub.add_parser("list", help="list starter requests")
    args = parser.parse_args(argv)

    repo = default_repository()
    if args.command == "list":
        for rid, r in repo.requests.items():
            print(f"{rid}  {r.get('product_name')}  ({r.get('vendor_name')}, {r.get('annual_cost_usd')})")
        return 0
    try:
        request = repo.get_request(args.request_id)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2
    result = analyze(request, args.arch, repo=repo, fault=args.fault,
                     case_key=(args.request_id, representative_run(args.arch, args.request_id)))
    d = result.decision
    if args.json:
        print(json.dumps(d.model_dump(mode="json"), indent=2))
    else:
        print(f"{d.request_id}  [{d.meta.architecture} | {d.meta.model}]  -> {d.recommendation.value}")
        print(f"  summary:   {d.summary}")
        print(f"  next step: {d.next_step.owner_role}: {d.next_step.action} - {d.next_step.detail}")
        print(f"  approvals: {', '.join(f'{a.role} ({a.rule_id})' for a in d.approvals_required)}")
        print(f"  flags:     {', '.join(f'{f.code}/{f.severity}' for f in d.risk_flags)}")
        print(f"  missing:   {', '.join(m.field for m in d.missing_information) or '-'}")
        print(f"  handoff:   {d.human_handoff.required} -> {d.human_handoff.assigned_role}")
        print(f"  evidence:  {len(d.evidence)} items; overrides: {len(d.overrides)}")
        print(f"  calls:     {d.meta.llm_calls} LLM, {d.meta.tool_calls} tool, {d.meta.latency_ms:.0f} ms"
              + (" (replayed)" if d.meta.replayed else "") + (f"; warnings: {d.meta.warnings}" if d.meta.warnings else ""))
    if args.trace:
        print(json.dumps(result.trace, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
