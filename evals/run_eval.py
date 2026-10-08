"""Evaluation harness: runs architectures A, B and R on the same labelled cases and writes the results.

    python -m evals.run_eval --arch A B R --runs 3 --workers 6          # LLM_MODE=record: live run, saves cassettes
    LLM_MODE=replay python -m evals.run_eval --arch A B R --runs 3 --check   # offline; must reproduce results exactly

Every architecture gets identical cases, data fixtures, fault injection, model, temperature, tools, policy engine and
guardrails; each case-run builds fresh state (repository, in-process vendor service, trace). Results go to
evals/runs/<timestamp>/ (git-ignored, full traces) and evals/results/ (committed: results.json, summary.json,
summary.md, per_case.csv). Replay reports the recorded latencies, so its numbers match the recorded run.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from evals.metrics import HEADLINE, aggregate, per_tag, run_metrics, score_case, stability
from mock_api.app import create_app
from src.agents.prompts import PROMPT_VERSION
from src.config import ROOT, get_settings
from src.data_access import Repository
from src.orchestrator import analyze
from src.schemas import PurchaseRequest

CASES_FILE = ROOT / "evals" / "cases.yaml"
RESULTS = ROOT / "evals" / "results"
RUNS = ROOT / "evals" / "runs"
FAULTS = {None: None, "down": "down", "vendor_down": "down", "slow": "slow", "vendor_slow": "slow"}


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    return [c for c in cases if not only or c["id"] in only]


def build_request(case: dict[str, Any], repo: Repository) -> PurchaseRequest:
    if case.get("starter_request_id"):
        return repo.get_request(case["starter_request_id"])
    return PurchaseRequest.from_raw(case["request"])


def run_one(case: dict[str, Any], arch: str, run: int, settings) -> dict[str, Any]:
    repo = Repository(overlay=case.get("fixtures"))
    http = TestClient(create_app(repo.vendor_risk))  # the real mock service app, mounted in-process per case
    request = build_request(case, repo)
    result = analyze(request, arch, repo=repo, settings=settings, fault=FAULTS[case.get("fault")], http=http,
                     case_key=(case["id"], run))
    decision = result.decision.model_dump(mode="json")
    _rules_latency(settings, arch, case["id"], run, decision)
    return {"case_id": case["id"], "architecture": arch, "run": run, "decision": decision,
            "raw_output": result.raw_output, "trace": result.trace,
            "approvals": [a["role"] for a in decision["approvals_required"]]}


def _rules_latency(settings, arch: str, case_id: str, run: int, decision: dict[str, Any]) -> None:
    """R has no LLM cassette; record/replay its latency in a tiny file so replay reproduces every number."""
    if arch != "R" or settings.llm_mode not in ("record", "replay"):
        return
    path = settings.cassette_dir / "R" / case_id / f"run{run}.json"
    if settings.llm_mode == "record":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"run_latency_ms": decision["meta"]["latency_ms"]}), encoding="utf-8")
    elif path.is_file():
        decision["meta"]["latency_ms"] = json.loads(path.read_text(encoding="utf-8"))["run_latency_ms"]
        decision["meta"]["replayed"] = True


def summarise(rows: list[dict], cases: list[dict], archs: list[str], runs: int, settings, workers: int,
              wall_s: float) -> dict[str, Any]:
    by_id = {c["id"]: c for c in cases}
    prices = (settings.price_in_per_1m, settings.price_out_per_1m)
    overall, tags, failures = {}, {}, []
    for arch in archs:
        arch_rows = [r for r in rows if r["architecture"] == arch]
        per_run = [run_metrics([r for r in arch_rows if r["run"] == n], by_id, prices) for n in range(1, runs + 1)]
        overall[arch] = aggregate(per_run)
        overall[arch]["stability"] = {"mean": stability(arch_rows), "std": None}
        tags[arch] = per_tag(arch_rows, by_id)
        for r in arch_rows:
            s = r["score"]
            if not s["all_checks_pass"]:
                exp = by_id[r["case_id"]]["expected"]
                failures.append({
                    "architecture": arch, "case_id": r["case_id"], "run": r["run"],
                    "expected": exp["recommendation"] + (f" (or {', '.join(exp['acceptable'])})" if exp.get("acceptable") else ""),
                    "actual": s["rec"], "agent_raw": s["raw_rec"],
                    "problems": _problems(s, exp, r),
                })
    all_tags = sorted({t for c in cases for t in c["tags"]})
    per_tag_table = {t: {"n": sum(1 for c in cases if t in c["tags"]),
                         **{a: tags[a].get(t) for a in archs}} for t in all_tags}
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {"model": settings.llm_model, "llm_mode": settings.llm_mode, "temperature": settings.llm_temperature,
                   "prompt_version": PROMPT_VERSION, "runs": runs, "workers": workers, "cases": len(cases),
                   "architectures": archs, "as_of": settings.as_of.isoformat(), "wall_clock_s": round(wall_s, 1)},
        "note": (f"{len(cases)} cases x {runs} run(s) per architecture, model {settings.llm_model}, temperature "
                 f"{settings.llm_temperature}, {workers} parallel workers (latency includes queueing at the provider). "
                 "Mean ± population std over runs. n is small: a 1-2 case difference (3-6 points) is within noise."),
        "headline_metrics": [{"key": k, "label": label} for k, label in HEADLINE],
        "overall": overall,
        "per_tag": per_tag_table,
        "failures": sorted(failures, key=lambda f: (f["architecture"], f["case_id"], f["run"])),
    }


def _problems(s: dict, exp: dict, row: dict) -> list[str]:
    out = []
    if not s["rec_correct"]:
        out.append(f"recommendation {s['rec']}")
    if not s["approvals_exact"]:
        out.append(f"approvals missing {s['approvals_missing']} extra {s['approvals_extra']}")
    if s["flags_missing"]:
        out.append(f"flags missing {s['flags_missing']}")
    if s["must_not_violations"]:
        out.append("forbidden flag raised")
    if s["missing_recall"] not in (None, 1.0):
        out.append("missing-info recall < 1")
    if not s["handoff_correct"]:
        out.append(f"handoff {s['handoff']} (expected {exp['handoff_required']})")
    if not s["owner_correct"]:
        out.append(f"next owner {row['decision']['next_step']['owner_role']} not in {exp['next_owner_role']}")
    if s["failsafe"]:
        out.append("fail-safe: " + "; ".join(row["decision"]["meta"].get("warnings", []))[:160])
    return out


def _fmt(v: dict | None, key: str) -> str:
    if not v or v.get("mean") is None:
        return "n/a"
    m, sd = v["mean"], v.get("std")
    pct = key.endswith(("accuracy", "rate", "recall", "precision", "exact", "adherence", "resistance", "control",
                        "correct", "pass")) or key in ("stability",)
    if key.startswith("cost"):
        return f"${m:.5f}"
    if pct:
        body = f"{100 * m:.1f}%" + (f" ± {100 * sd:.1f}" if sd else "")
    elif key.startswith("latency"):
        body = f"{m:,.0f}" + (f" ± {sd:,.0f}" if sd else "")
    else:
        body = f"{m:.2f}" + (f" ± {sd:.2f}" if sd else "")
    return body


def write_markdown(summary: dict, path: Path) -> None:
    archs = summary["config"]["architectures"]
    names = {"A": "A · single agent", "B": "B · staged (2 agents)", "R": "R · rules only"}
    lines = [f"# Evaluation summary\n\n{summary['note']}\n",
             f"Model `{summary['config']['model']}`, prompt version `{summary['config']['prompt_version']}`, "
             f"reference date {summary['config']['as_of']}. Generated by `python -m evals.run_eval`.\n",
             "## Headline: A vs B vs R\n", "| Metric | " + " | ".join(names[a] for a in archs) + " |",
             "|---|" + "---:|" * len(archs)]
    for m in summary["headline_metrics"]:
        lines.append(f"| {m['label']} | " + " | ".join(_fmt(summary["overall"][a].get(m["key"]), m["key"])
                                                      for a in archs) + " |")
    lines += ["\n## Final recommendation accuracy by category\n",
              "| Category | n | " + " | ".join(names[a] for a in archs) + " |", "|---|---:|" + "---:|" * len(archs)]
    for tag, row in summary["per_tag"].items():
        lines.append(f"| {tag} | {row['n']} | " + " | ".join("n/a" if row[a] is None else f"{100 * row[a]:.0f}%"
                                                              for a in archs) + " |")
    lines += ["\n## Every failure (expected vs actual)\n",
              "| Arch | Case | Run | Expected | Final | Agent (pre-guardrail) | Problems |", "|---|---|---:|---|---|---|---|"]
    for f in summary["failures"]:
        lines.append(f"| {f['architecture']} | {f['case_id']} | {f['run']} | {f['expected']} | {f['actual']} | "
                     f"{f['agent_raw'] or '-'} | {'; '.join(f['problems'])} |")
    if not summary["failures"]:
        lines.append("| - | - | - | - | - | - | none |")
    lines += ["\n## Uncertainty\n",
              f"{summary['config']['cases']} cases, {summary['config']['runs']} run(s) each at temperature "
              f"{summary['config']['temperature']}. One case is ~{100 / summary['config']['cases']:.1f} points of "
              "accuracy, so differences of 1-2 cases are within noise; the ± values show run-to-run spread."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", nargs="+", default=["A", "B", "R"], choices=["A", "B", "R"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cases", nargs="*", help="only these case IDs")
    parser.add_argument("--check", action="store_true", help="fail unless results equal the committed summary")
    parser.add_argument("--no-write", action="store_true", help="do not refresh evals/results")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.llm_mode == "live" and settings.llm_configured:
        print("Note: LLM_MODE=live does not save cassettes; use LLM_MODE=record for a reproducible run.")
    if settings.llm_mode == "replay":
        settings = dataclasses.replace(settings, llm_api_key="")
    cases = load_cases(args.cases)
    jobs = [(c, a, n) for a in args.arch for n in range(1, args.runs + 1) for c in cases]
    print(f"Running {len(jobs)} case-runs ({len(cases)} cases x {args.runs} runs x {args.arch}) "
          f"mode={settings.llm_mode} model={settings.llm_model} workers={args.workers}")
    start = time.perf_counter()
    rows: list[dict] = []
    by_id = {c["id"]: c for c in cases}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, c, a, n, settings): (c["id"], a, n) for c, a, n in jobs}
        for i, fut in enumerate(as_completed(futures), start=1):
            row = fut.result()
            row["score"] = score_case(by_id[row["case_id"]], row["decision"])
            rows.append(row)
            s = row["score"]
            print(f"[{i:>3}/{len(jobs)}] {row['architecture']} run{row['run']} {row['case_id']:<16} "
                  f"{s['rec']:<18} {'PASS' if s['all_checks_pass'] else 'fail'}  {s['latency_ms']:>8.0f} ms", flush=True)
    wall = time.perf_counter() - start
    rows.sort(key=lambda r: (r["architecture"], r["case_id"], r["run"]))
    summary = summarise(rows, cases, args.arch, args.runs, settings, args.workers, wall)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rows.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    if args.check:
        committed = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))
        same = (committed["overall"] == summary["overall"] and committed["per_tag"] == summary["per_tag"]
                and committed["failures"] == summary["failures"])
        print("REPLAY CHECK:", "results reproduce the committed summary exactly" if same else "MISMATCH")
        if not same:
            for arch in summary["overall"]:
                for k, v in summary["overall"][arch].items():
                    if committed["overall"].get(arch, {}).get(k) != v:
                        print(f"  {arch}.{k}: committed={committed['overall'].get(arch, {}).get(k)} now={v}")
            return 1
    elif not args.no_write:
        RESULTS.mkdir(parents=True, exist_ok=True)
        slim = [{k: r[k] for k in ("case_id", "architecture", "run", "score", "decision", "raw_output")} for r in rows]
        (RESULTS / "results.json").write_text(json.dumps(slim, indent=1, default=str), encoding="utf-8")
        (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        write_markdown(summary, RESULTS / "summary.md")
        with (RESULTS / "per_case.csv").open("w", newline="", encoding="utf-8") as f:
            cols = ["architecture", "case_id", "run", "rec", "raw_rec", "rec_correct", "approvals_exact",
                    "owner_correct", "handoff_correct", "flag_recall", "missing_recall", "overrides",
                    "evidence_grounded", "evidence_total", "failsafe", "all_checks_pass", "latency_ms", "llm_calls",
                    "tool_calls", "tokens_in", "tokens_out"]
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for r in rows:
                writer.writerow({"architecture": r["architecture"], "case_id": r["case_id"], "run": r["run"],
                                 **{c: r["score"].get(c) for c in cols[3:]}})
    print(f"\nDone in {wall:.0f}s. Full traces: {run_dir.relative_to(ROOT)}")
    for arch in args.arch:
        o = summary["overall"][arch]
        print(f"  {arch}: accuracy {_fmt(o['rec_accuracy'], 'rec_accuracy')}, all checks "
              f"{_fmt(o['all_checks_pass'], 'all_checks_pass')}, under-escalation {o['under_escalation']['mean']}, "
              f"p50 {_fmt(o['latency_p50_ms'], 'latency_p50_ms')} ms, LLM calls {_fmt(o['llm_calls_mean'], 'x')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
