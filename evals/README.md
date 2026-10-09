# Public evaluation harness

> **Full A/B evaluation:** the project's own harness (`python -m evals.run_eval`) runs 37 labelled cases
> (`cases.yaml`) on A, B and a rules-only baseline with record/replay — results in `results/summary.md`.
> The six public cases below run through the starter adapter and pass 6/6 for both architectures (checked in CI).

The six public cases are intentionally visible. Use them to test both architectures while you develop.

Run:

```bash
python evals/run_public_evals.py --architecture single
python evals/run_public_evals.py --architecture staged
```

The runner:
- calls `src.solution.handle_request(request_id, architecture)`,
- validates the `ProcurementDecision` schema,
- checks a few minimum expectations,
- measures end-to-end latency,
- exports a CSV result file.

## Important limitations

Passing these checks does **not** guarantee a high score. The assessment also considers:
- whether evidence is actually grounded in tool outputs,
- tool/agent boundaries,
- deterministic vs. probabilistic decisions,
- failure handling,
- architecture quality,
- evaluation reasoning,
- hidden cases.

Do not tune your implementation to request IDs. Hidden cases use different records and values.

## Comparison

Use the same case set for both architectures. Your final evaluation should include at least:

| Metric | Single | Staged / 2-agent |
|---|---:|---:|
| Public cases meeting minimum expectations |  |  |
| Avg latency (ms) |  |  |
| Avg LLM calls |  |  |
| Avg tool calls |  |  |
| Policy failures found manually |  |  |

The public runner can measure latency. LLM/tool counts must come from your own telemetry or the optional telemetry field in the output contract.
